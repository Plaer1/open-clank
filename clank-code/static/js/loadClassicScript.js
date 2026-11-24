// Load classic bundles in their original global scope, once per URL.
const pending = new Map();

export function loadClassicScript(url) {
  if (!pending.has(url)) {
    const script = document.createElement('script');
    script.src = url;
    const loaded = new Promise((resolve, reject) => {
      script.onload = resolve;
      script.onerror = () => {
        script.remove();
        pending.delete(url);
        reject(new Error(`Could not load ${url}`));
      };
    });
    pending.set(url, loaded);
    document.head.append(script);
  }
  return pending.get(url);
}
