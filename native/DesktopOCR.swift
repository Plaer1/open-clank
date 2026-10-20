import Foundation
import ImageIO
import Vision

struct OCRBox: Codable {
    let text: String
    let confidence: Float
    let candidates: [String]
    let x: Double
    let y: Double
    let width: Double
    let height: Double
    let order: Int
}

struct OCRResult: Codable {
    let supportedLanguages: [String]
    let width: Int
    let height: Int
    let boxes: [OCRBox]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count == 2 else { fail("usage: DesktopOCR <image>") }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { fail("image unavailable") }
let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
let orientationRaw = (properties?[kCGImagePropertyOrientation] as? NSNumber)?.uint32Value ?? 1
let orientation = CGImagePropertyOrientation(rawValue: orientationRaw) ?? .up

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let languages = (try? request.supportedRecognitionLanguages()) ?? []
let handler = VNImageRequestHandler(cgImage: image, orientation: orientation, options: [:])
do { try handler.perform([request]) } catch { fail("vision OCR failed: \(error)") }

let observations = (request.results ?? []).sorted {
    let a = $0.boundingBox
    let b = $1.boundingBox
    if abs(a.minY - b.minY) > 0.02 { return a.minY > b.minY }
    return a.minX < b.minX
}
let boxes = observations.enumerated().map { index, observation in
    let candidates = observation.topCandidates(3)
    let box = observation.boundingBox
    return OCRBox(
        text: candidates.first?.string ?? "",
        confidence: candidates.first?.confidence ?? 0,
        candidates: candidates.map(\.string),
        x: box.minX * Double(image.width),
        y: (1 - box.maxY) * Double(image.height),
        width: box.width * Double(image.width),
        height: box.height * Double(image.height),
        order: index
    )
}
let result = OCRResult(supportedLanguages: languages, width: image.width, height: image.height, boxes: boxes)
do {
    let data = try JSONEncoder().encode(result)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
} catch { fail("OCR encoding failed: \(error)") }
