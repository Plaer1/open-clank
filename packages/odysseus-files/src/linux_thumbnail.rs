//! Linux content provider: validated shared cache, then optional KIO sidecar.
//! No desktop shared libraries are linked into the broker or this helper.
use crate::wire::{HelperErrorCode as Error, RenderCommand, MAX_HELPER_OUTPUT_BYTES};
use std::fs::{self, File};
use std::io::{Cursor, Read};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub fn adapter_path() -> Option<PathBuf> {
    std::env::var_os("ODYSSEUS_KDE_THUMBNAIL_ADAPTER").map(PathBuf::from).or_else(|| {
        std::env::current_exe().ok().and_then(|p| p.parent().map(|p| p.join("odysseus-kde-thumbnail-adapter")))
    })
}

fn host_command(path: &Path) -> Command {
    let mut command = Command::new(path);
    // PyInstaller's private libraries belong to bundled workers, never Qt/KIO
    // executables that are part of the host desktop.
    match std::env::var_os("LD_LIBRARY_PATH_ORIG") {
        Some(original) => { command.env("LD_LIBRARY_PATH", original); }
        None => { command.env_remove("LD_LIBRARY_PATH"); }
    }
    command.env_remove("ODYSSEUS_QUICKLOOK_SESSION_TOKEN");
    command
}

pub fn probe() -> bool {
    let Some(path) = adapter_path() else { return false; };
    let Ok(mut child) = host_command(&path).arg("--probe").stdin(Stdio::null())
        .stdout(Stdio::null()).stderr(Stdio::null()).spawn() else { return false; };
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(10)),
            _ => { let _ = child.kill(); let _ = child.wait(); return false; }
        }
    }
}

pub fn file_uri(path: &Path) -> String {
    let mut uri = String::from("file://");
    const HEX: &[u8] = b"0123456789ABCDEF";
    for &byte in path.as_os_str().as_bytes() {
        if byte.is_ascii_alphanumeric() || b"/-._~".contains(&byte) {
            uri.push(byte as char);
        } else {
            uri.push('%'); uri.push(HEX[(byte >> 4) as usize] as char); uri.push(HEX[(byte & 15) as usize] as char);
        }
    }
    uri
}

fn cache_root() -> Option<PathBuf> {
    std::env::var_os("XDG_CACHE_HOME").map(PathBuf::from).filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".cache")))
        .map(|p| p.join("thumbnails"))
}

fn identity(m: &fs::Metadata) -> (u64,u64,u64,i64,i64,i64,i64) {
    (m.dev(),m.ino(),m.len(),m.mtime(),m.mtime_nsec(),m.ctime(),m.ctime_nsec())
}

fn decode(bytes: &[u8], width: u32, height: u32, expected: Option<(&str, &fs::Metadata)>) -> Result<Vec<u8>, Error> {
    if bytes.len() > MAX_HELPER_OUTPUT_BYTES { return Err(Error::OutputTooLarge); }
    let mut decoder = png::Decoder::new_with_limits(Cursor::new(bytes), png::Limits { bytes: 64*1024*1024 });
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = decoder.read_info().map_err(|_| Error::EncodeFailed)?;
    if reader.info().width == 0 || reader.info().height == 0 || reader.info().width > 3072 || reader.info().height > 3072 {
        return Err(Error::OutputTooLarge);
    }
    let mut pixels = vec![0; reader.output_buffer_size()];
    let frame = reader.next_frame(&mut pixels).map_err(|_| Error::EncodeFailed)?;
    reader.finish().map_err(|_| Error::EncodeFailed)?;
    if let Some((uri, metadata)) = expected {
        let info = reader.info();
        let mut texts = std::collections::BTreeMap::new();
        // Only three standard metadata fields affect admission. Never inflate
        // arbitrary per-image text fields: each could expand independently of
        // the PNG decoder's pixel allocation limit.
        let required = |key: &str| matches!(key,"Thumb::URI"|"Thumb::MTime"|"Thumb::Size");
        for text in &info.uncompressed_latin1_text {
            if !required(&text.keyword) { continue; }
            if texts.insert(text.keyword.clone(), text.text.clone()).is_some() { return Err(Error::EncodeFailed); }
        }
        for text in &info.compressed_latin1_text {
            if !required(&text.keyword) { continue; }
            let mut text = text.clone();
            text.decompress_text_with_limit(16*1024).map_err(|_| Error::EncodeFailed)?;
            if texts.insert(text.keyword.clone(), text.get_text().map_err(|_| Error::EncodeFailed)?).is_some() { return Err(Error::EncodeFailed); }
        }
        for text in &info.utf8_text {
            if !required(&text.keyword) { continue; }
            let mut text = text.clone();
            text.decompress_text_with_limit(16*1024).map_err(|_| Error::EncodeFailed)?;
            if texts.insert(text.keyword.clone(), text.get_text().map_err(|_| Error::EncodeFailed)?).is_some() { return Err(Error::EncodeFailed); }
        }
        if texts.get("Thumb::URI").map(String::as_str) != Some(uri)
            || texts.get("Thumb::MTime").and_then(|s| s.parse::<i64>().ok()) != Some(metadata.mtime())
            || texts.get("Thumb::Size").is_some_and(|s| s.parse::<u64>().ok() != Some(metadata.len())) {
            return Err(Error::NativeUnavailable);
        }
    }
    // Normalize decoded pixels so cache metadata never reaches the browser and
    // a valid but large desktop tier respects the admitted requested bounds.
    let channels = match frame.color_type {
        png::ColorType::Grayscale => 1,
        png::ColorType::GrayscaleAlpha => 2,
        png::ColorType::Rgb => 3,
        png::ColorType::Rgba => 4,
        _ => return Err(Error::EncodeFailed),
    };
    let ratio = (width as f64 / frame.width as f64).min(height as f64 / frame.height as f64).min(1.0);
    let w = ((frame.width as f64 * ratio).floor() as u32).max(1);
    let h = ((frame.height as f64 * ratio).floor() as u32).max(1);
    let mut rgba = Vec::with_capacity(w as usize*h as usize*4);
    for y in 0..h { for x in 0..w {
        let offset = ((y as u64*frame.height as u64/h as u64)*frame.width as u64 + x as u64*frame.width as u64/w as u64) as usize*channels;
        let pixel = &pixels[offset..offset+channels];
        match channels {
            1 => rgba.extend_from_slice(&[pixel[0],pixel[0],pixel[0],255]),
            2 => rgba.extend_from_slice(&[pixel[0],pixel[0],pixel[0],pixel[1]]),
            3 => rgba.extend_from_slice(&[pixel[0],pixel[1],pixel[2],255]),
            _ => rgba.extend_from_slice(pixel),
        }
    }}
    let mut output = Vec::new();
    { let mut encoder = png::Encoder::new(&mut output,w,h);
      encoder.set_color(png::ColorType::Rgba); encoder.set_depth(png::BitDepth::Eight);
      encoder.write_header().map_err(|_| Error::EncodeFailed)?.write_image_data(&rgba).map_err(|_| Error::EncodeFailed)?; }
    Ok(output)
}

fn cached(root: &Path, source: &Path, metadata: &fs::Metadata, width: u32, height: u32) -> Option<Vec<u8>> {
    if source.starts_with(root) { return None; }
    let uri = file_uri(source);
    let key = format!("{:x}.png", md5::compute(uri.as_bytes()));
    let tiers = [(128,"normal"),(256,"large"),(512,"x-large"),(1024,"xx-large")];
    let wanted = width.max(height);
    // Prefer a sufficient tier, then fall back to a smaller genuine preview.
    let mut candidates: Vec<_> = tiers.into_iter().collect();
    candidates.sort_by_key(|(size,_)| (if *size >= wanted {0} else {1}, if *size >= wanted {*size} else {1024-*size}));
    for (_,tier) in candidates {
        let path = root.join(tier).join(&key);
        let Ok(file) = File::open(&path) else { continue; };
        let Ok(meta) = file.metadata() else { continue; };
        if !meta.is_file() || meta.len() > MAX_HELPER_OUTPUT_BYTES as u64
            || (meta.mtime(),meta.mtime_nsec()) < (metadata.mtime(),metadata.mtime_nsec())
            || (meta.mtime(),meta.mtime_nsec()) < (metadata.ctime(),metadata.ctime_nsec()) { continue; }
        let mut bytes = Vec::new();
        if file.take((MAX_HELPER_OUTPUT_BYTES+1) as u64).read_to_end(&mut bytes).is_err() { continue; }
        if let Ok(png) = decode(&bytes,width,height,Some((&uri,metadata))) { return Some(png); }
    }
    None
}

pub fn render(request: RenderCommand) -> Result<Vec<u8>, Error> {
    if request.width == 0 || request.height == 0 || request.width > 1024 || request.height > 1024
        || !request.scale.is_finite() || !(1.0..=3.0).contains(&request.scale)
        || request.max_output_bytes == 0 || request.max_output_bytes as usize > MAX_HELPER_OUTPUT_BYTES
        || request.path.contains(&0) { return Err(Error::InvalidRequest); }
    let path = PathBuf::from(std::ffi::OsString::from_vec(request.path));
    if !path.is_absolute() || fs::canonicalize(&path).ok().as_ref() != Some(&path) { return Err(Error::NotRegularFile); }
    // Reading the source is a prerequisite even for cache hits.
    let file = File::open(&path).map_err(|_| Error::NotRegularFile)?;
    let metadata = file.metadata().map_err(|_| Error::NotRegularFile)?;
    if !metadata.is_file() { return Err(Error::NotRegularFile); }
    let width = (request.width as f64*request.scale).ceil() as u32;
    let height = (request.height as f64*request.scale).ceil() as u32;
    let root = cache_root();
    if root.as_ref().is_some_and(|r| path.starts_with(r)) { return Err(Error::NativeUnavailable); }
    let cached_png = root.as_ref().and_then(|root| cached(root,&path,&metadata,width,height));
    let output = match cached_png {
        Some(bytes) => bytes,
        None => {
            let adapter = adapter_path().ok_or(Error::NativeUnavailable)?;
            let uri = file_uri(&path);
            let key = format!("{:x}.png",md5::compute(uri.as_bytes()));
            let rejected_cache_present = root.as_ref().is_some_and(|root| ["normal","large","x-large","xx-large"].into_iter().any(|tier| root.join(tier).join(&key).exists()));
            let mut command = host_command(&adapter);
            command.arg(&path).arg(width.to_string()).arg(height.to_string())
                .arg(MAX_HELPER_OUTPUT_BYTES.to_string()).stdin(Stdio::null()).stdout(Stdio::piped())
                .stderr(Stdio::null());
            if rejected_cache_present { command.arg("--fresh"); }
            let mut child = command.spawn().map_err(|_| Error::NativeUnavailable)?;
            // Bounded read; outer broker kills our complete process group on
            // deadline, including native children holding inherited handles.
            let mut bytes = Vec::new();
            let read = child.stdout.take().ok_or(Error::NativeUnavailable)?.take((MAX_HELPER_OUTPUT_BYTES+1) as u64).read_to_end(&mut bytes);
            if bytes.len() > MAX_HELPER_OUTPUT_BYTES { let _ = child.kill(); let _ = child.wait(); return Err(Error::OutputTooLarge); }
            let status = child.wait().map_err(|_| Error::NativeUnavailable)?;
            if read.is_err() || !status.success() { return Err(Error::NativeUnavailable); }
            decode(&bytes,width,height,None)?
        }
    };
    if identity(&metadata) != identity(&fs::metadata(&path).map_err(|_| Error::NotRegularFile)?)
        || fs::canonicalize(&path).ok().as_ref() != Some(&path) { return Err(Error::NotRegularFile); }
    if output.len() > request.max_output_bytes as usize { return Err(Error::OutputTooLarge); }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;
    fn cache_png(path: &Path, uri: &str, mtime: i64, size: Option<u64>, dimensions: (u32,u32)) {
        let mut encoder = png::Encoder::new(File::create(path).unwrap(),dimensions.0,dimensions.1);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        encoder.add_text_chunk("Thumb::URI".into(),uri.into()).unwrap();
        encoder.add_text_chunk("Thumb::MTime".into(),mtime.to_string()).unwrap();
        if let Some(size) = size { encoder.add_text_chunk("Thumb::Size".into(),size.to_string()).unwrap(); }
        encoder.write_header().unwrap().write_image_data(&vec![180;dimensions.0 as usize*dimensions.1 as usize*4]).unwrap();
    }
    #[test]
    fn canonical_uri_and_real_cache_pixels_cover_reserved_and_unicode_names() {
        let dir = tempdir().unwrap(); let root = dir.path().join("cache");
        fs::create_dir_all(root.join("normal")).unwrap();
        for name in ["space name.png","é雪.png","percent%.png","hash#.png","query?.png","[brackets].png","Case.png","case.png"] {
            let source = dir.path().join(name); fs::write(&source,b"source").unwrap();
            let metadata = fs::metadata(&source).unwrap(); let uri = file_uri(&source);
            assert!(!uri.contains([' ','[',']','#','?']));
            let cached_path = root.join("normal").join(format!("{:x}.png",md5::compute(&uri)));
            cache_png(&cached_path,&uri,metadata.mtime(),Some(metadata.len()),(64,32));
            let result = cached(&root,&source,&metadata,16,16).unwrap();
            let reader = png::Decoder::new(Cursor::new(result)).read_info().unwrap();
            assert_eq!((reader.info().width,reader.info().height),(16,8));
        }
        assert_eq!(file_uri(Path::new("/tmp/a [é]%#?.png")),"file:///tmp/a%20%5B%C3%A9%5D%25%23%3F.png");
    }
    #[test]
    fn stale_wrong_uri_size_corrupt_and_oversized_entries_are_misses() {
        let dir = tempdir().unwrap(); let root = dir.path().join("cache"); fs::create_dir_all(root.join("normal")).unwrap();
        let source = dir.path().join("source"); fs::write(&source,b"source").unwrap();
        let metadata = fs::metadata(&source).unwrap(); let uri = file_uri(&source);
        let entry = root.join("normal").join(format!("{:x}.png",md5::compute(&uri)));
        for (candidate_uri,time,size) in [("file:///other",metadata.mtime(),Some(metadata.len())),(uri.as_str(),metadata.mtime()-1,None),(uri.as_str(),metadata.mtime()+1,None),(uri.as_str(),metadata.mtime(),Some(1))] {
            cache_png(&entry,candidate_uri,time,size,(1,1));
            assert!(cached(&root,&source,&metadata,128,128).is_none());
        }
        cache_png(&entry,&uri,metadata.mtime(),None,(1,1));
        assert!(cached(&root,&source,&metadata,128,128).is_some());
        fs::write(&entry,b"not a png").unwrap(); assert!(cached(&root,&source,&metadata,128,128).is_none());
        File::create(&entry).unwrap().set_len((MAX_HELPER_OUTPUT_BYTES+1) as u64).unwrap();
        assert!(cached(&root,&source,&metadata,128,128).is_none());
    }
    #[test]
    fn same_second_same_size_edit_invalidates_older_cache() {
        use std::time::{SystemTime,UNIX_EPOCH};
        let dir=tempdir().unwrap(); let root=dir.path().join("cache");fs::create_dir_all(root.join("normal")).unwrap();
        let source=dir.path().join("edited"); fs::write(&source,b"before").unwrap();
        let second=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs()-2;
        let times=fs::FileTimes::new().set_modified(UNIX_EPOCH+Duration::new(second,100));
        File::options().write(true).open(&source).unwrap().set_times(times).unwrap();
        let metadata=fs::metadata(&source).unwrap();let uri=file_uri(&source);
        let entry=root.join("normal").join(format!("{:x}.png",md5::compute(&uri)));
        cache_png(&entry,&uri,metadata.mtime(),Some(metadata.len()),(1,1));
        assert!(cached(&root,&source,&metadata,32,32).is_some());
        fs::write(&source,b"after!").unwrap();
        File::options().write(true).open(&source).unwrap().set_times(fs::FileTimes::new().set_modified(UNIX_EPOCH+Duration::new(second,300))).unwrap();
        let after=fs::metadata(&source).unwrap();assert_eq!(after.len(),metadata.len());assert_eq!(after.mtime(),metadata.mtime());
        assert!(cached(&root,&source,&after,32,32).is_none());
    }
    #[test]
    fn cache_metadata_skips_irrelevant_compressed_text_and_rejects_required_duplicates() {
        let dir=tempdir().unwrap();let source=dir.path().join("source");fs::write(&source,b"source").unwrap();
        let metadata=fs::metadata(&source).unwrap();let uri=file_uri(&source);
        for duplicate in [false,true] {
            let mut bytes=Vec::new();
            {
                let mut encoder=png::Encoder::new(&mut bytes,1,1);
                encoder.set_color(png::ColorType::Rgba);encoder.set_depth(png::BitDepth::Eight);
                encoder.add_text_chunk("Thumb::URI".into(),uri.clone()).unwrap();
                encoder.add_text_chunk("Thumb::MTime".into(),metadata.mtime().to_string()).unwrap();
                // Much larger than the per-required-field decompression cap.
                // It must be ignored, without affecting a legitimate hit.
                encoder.add_ztxt_chunk("Unrelated::Description".into(),"x".repeat(256*1024)).unwrap();
                if duplicate { encoder.add_ztxt_chunk("Thumb::URI".into(),uri.clone()).unwrap(); }
                encoder.write_header().unwrap().write_image_data(&[200,30,70,255]).unwrap();
            }
            let result=decode(&bytes,32,32,Some((&uri,&metadata)));
            if duplicate { assert_eq!(result,Err(Error::EncodeFailed)); }
            else { assert!(result.is_ok()); }
        }
    }
    #[test]
    fn malformed_pixels_and_missing_or_deleted_sources_are_rejected() {
        let dir = tempdir().unwrap(); let path = dir.path().join("deleted.png");
        let request = || RenderCommand { session_token:[0;32],job_id:1,width:32,height:32,scale:1.0,max_output_bytes:1024,path:path.as_os_str().as_bytes().to_vec() };
        fs::write(&path,b"source").unwrap(); fs::remove_file(&path).unwrap();
        assert_eq!(render(request()),Err(Error::NotRegularFile));
        assert_eq!(decode(b"invalid",32,32,None),Err(Error::EncodeFailed));
    }
}
