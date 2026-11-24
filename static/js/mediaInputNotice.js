// Plain-words state for an attachment whose image was not sent to the model (media-input-v1 receipt on the attachment record).
// Pure: no DOM. The words mirror REASON_TEXT / ALTERNATIVE in src/openclank/media_input_policy.py.
const WORDS = {
  too_large_bytes: 'the file is larger than the allowed size',
  too_many_pixels: 'the image has too many pixels to decode safely',
  side_too_long: 'one side of the image is longer than the allowed limit',
  unsupported_type: 'this image format is not one the model input policy accepts',
  type_mismatch: 'the file contents do not match its declared type',
  unreadable_header: 'the image header could not be read',
  route_without_vision: 'the selected model does not accept images',
};
const ALTERNATIVE = {
  route_without_vision: 'Choose a model that accepts images, or use Caption to have it described.',
  unsupported_type: 'Convert it to PNG, JPEG, WebP or GIF, or use Caption to have it described.',
};
// Reasons that stop the image at the model request. An upload-time receipt carries no `sent`, so these decide.
const BLOCKING = new Set(['too_large_bytes', 'too_many_pixels', 'side_too_long', 'route_without_vision']);

export function mediaInputNotice(att) {
  const receipt = att && att.media_input;
  if (!receipt || typeof receipt !== 'object' || receipt.outcome !== 'omit' || !Object.prototype.hasOwnProperty.call(WORDS, receipt.reason)) return null;
  if (receipt.sent === true || (receipt.sent === undefined && !BLOCKING.has(receipt.reason))) return null;
  const alt = ALTERNATIVE[receipt.reason];
  return `Not sent to the model: ${WORDS[receipt.reason]}.${alt ? ' ' + alt : ''}`;
}
