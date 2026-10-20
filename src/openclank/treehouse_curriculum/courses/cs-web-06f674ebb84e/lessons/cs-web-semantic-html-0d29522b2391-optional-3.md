Save both files in one fresh owned folder. No JavaScript, server or remote assets are needed.

**index.html**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>My practice cards</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <h1>My practice cards</h1>
    <nav aria-label="On this page">
      <a href="#cards">Read the cards</a>
      <a href="#about">About this page</a>
    </nav>
  </header>
  <main>
    <section aria-labelledby="name-heading">
      <h2 id="name-heading">Your label</h2>
      <label for="name">Name</label>
      <input id="name" name="name" type="text" autocomplete="given-name">
      <p>This practice input stays on this page; it is not submitted.</p>
    </section>
    <section aria-labelledby="cards-heading" id="cards">
      <h2 id="cards-heading">Three small goals</h2>
      <div class="cards">
        <article class="card"><h3>Predict</h3><p>Write what you expect before a run.</p></article>
        <article class="card"><h3>Observe</h3><p>Record what actually happened.</p></article>
        <article class="card"><h3>Explain</h3><p>Connect the result to your prediction.</p></article>
      </div>
    </section>
    <section id="about" aria-labelledby="about-heading">
      <h2 id="about-heading">About</h2>
      <p>One page, one stylesheet, and three cards.</p>
    </section>
  </main>
</body>
</html>
```

**style.css**

```css
* { box-sizing: border-box; }
html { font-family: system-ui, sans-serif; line-height: 1.5; }
body { margin: 0; color: #111; background: #fff; }
header, main { max-width: 64rem; margin-inline: auto; padding: 1rem; }
nav { display: flex; flex-wrap: wrap; gap: 1rem; }
a { color: #0645ad; }
a:focus-visible, input:focus-visible {
  outline: 3px solid #111;
  outline-offset: 3px;
}
input { display: block; width: 100%; max-width: 24rem; font: inherit; padding: .5rem; }
section { margin-block: 1.5rem; }
.cards { display: grid; grid-template-columns: 1fr; gap: 1rem; }
.card { min-width: 0; border: 1px solid #333; border-radius: .5rem; padding: 1rem; }
.card p { overflow-wrap: anywhere; }
@media (min-width: 48rem) {
  .cards { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
```
