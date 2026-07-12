export function skillDescription(
  t: (key: string) => string,
  name: string,
  fallback?: string,
  bundled?: boolean,
) {
  if (!bundled) return fallback
  return t(`tui.skill.${name}.description`) || fallback
}
