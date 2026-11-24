const _CLANKER_GEM_OUTLINES = [
  [[0, -1], [.8, 0], [0, 1], [-.8, 0]],
  [[-.78, -.35], [-.18, -1], [.82, -.42], [.58, .72], [-.55, .9]],
  [[0, -1], [.9, .7], [-.9, .7]],
  [[-.72, -.58], [.42, -.88], [1, 0], [.35, .86], [-.82, .52]],
];

export function rasterGem(shard, radius, bright, colors, outline, opacity, pixelsPerUnit) {
  const half = Math.ceil(radius + (bright ? 15 : 3));
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = Math.min(144, Math.ceil(half * 2 * pixelsPerUnit));
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  const scale = canvas.width / (half * 2);
  ctx.setTransform(scale, 0, 0, scale, canvas.width / 2, canvas.height / 2);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  const shape = _CLANKER_GEM_OUTLINES[shard.shape];
  ctx.beginPath();
  ctx.moveTo(shape[0][0] * radius, shape[0][1] * radius);
  for (let i = 1; i < shape.length; i += 1) ctx.lineTo(shape[i][0] * radius, shape[i][1] * radius);
  ctx.closePath();
  ctx.fillStyle = colors[shard.color];
  ctx.globalAlpha = Math.min(1, opacity * (bright ? .62 : .22));
  if (bright) {
    ctx.shadowColor = colors[shard.color];
    ctx.shadowBlur = 9 * scale;
  }
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = outline;
  ctx.lineWidth = bright ? 2 : 1.4;
  ctx.globalAlpha = Math.min(1, opacity * (bright ? .8 : .34));
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(0, -radius * .7);
  ctx.lineTo(0, 0);
  ctx.lineTo(radius * .5, radius * .34);
  ctx.strokeStyle = colors[(shard.color + 2) % colors.length];
  ctx.lineWidth = .9;
  ctx.globalAlpha = Math.min(1, opacity * (bright ? .58 : .26));
  ctx.stroke();
  return { canvas, half };
}

const _CLANKER_NATIVE_DRIFT_EMOJI = [...'😀😎🥳🤖👻🎃😺💖💎🔥🌈⭐🌙🌞🌻🌵🍄🐶🐱🦊🐼🐸🐙🦋🐝🍎🍉🍒🍋🍓🍕🍩🍦🍪🎈🎉🎨🚀⚽🎸'];


export function nativeDriftSprite(cache, index) {
  const glyph = _CLANKER_NATIVE_DRIFT_EMOJI[index % _CLANKER_NATIVE_DRIFT_EMOJI.length];
  if (cache.has(glyph)) return cache.get(glyph);
  const image = document.createElement('canvas');
  image.width = image.height = 96;
  const ctx = image.getContext('2d');
  if (!ctx) return null;
  ctx.font = '70px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", emoji, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = '#ffd56c';
  ctx.fillText(glyph, 48, 48);
  const sprite = { image, width: image.width, height: image.height };
  cache.set(glyph, sprite);
  return sprite;
}
