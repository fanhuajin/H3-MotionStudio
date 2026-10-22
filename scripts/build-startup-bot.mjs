// Rebuild the native launcher's animation from the same MIT Bloub engine as StudioBot.
// Requires sharp for SVG rasterization; H3_SHARP_MODULE may point to an installed module.
import { build } from 'esbuild';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
const sharp = (await import(process.env.H3_SHARP_MODULE
  ? pathToFileURL(process.env.H3_SHARP_MODULE).href : 'sharp')).default;
const compiled = await build({
  stdin: { contents: 'export { BotEngine } from "./src/vendor/bloub/engine"; export * from "./src/vendor/bloub/repere";', resolveDir: process.cwd() },
  bundle: true, write: false, format: 'esm', platform: 'node',
});
const { BotEngine, RAYON, DEMI_VIEWBOX: vb } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);
const engine = new BotEngine(RAYON, 'wide');
const frames = [];
const size = 96, columns = 16, count = 240;
for (let i = 0; i < count; i++) {
  const time = i / 25;
  if (i === 60) engine.setState('idle', time);
  if (i === 100) engine.setState('orbit', time);
  if (i === 190) engine.setState('wink', time);
  if (i === 215) engine.setState('wide', time);
  const frame = engine.sample(time);
  const dots = frame.dots.map(dot => dot.d
    ? `<path d="${dot.d}" transform="translate(${dot.x} ${dot.y}) rotate(${dot.rot ?? 0}) scale(${RAYON})" opacity="${dot.opacity}"/>`
    : `<circle cx="${dot.x}" cy="${dot.y}" r="${dot.r}" opacity="${dot.opacity}"/>`).join('');
  const arcs = side => frame.arcs.map(arc => `<path d="${arc[side]}" stroke-width="${arc.width}" opacity="${arc.opacity * (side === 'back' ? .6 : 1)}"/>`).join('');
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="${-vb} ${-vb} ${vb * 2} ${vb * 2}">
    <defs><mask id="body" maskUnits="userSpaceOnUse" x="${-vb}" y="${-vb}" width="${vb * 2}" height="${vb * 2}">
      <path d="${frame.bodyPath}" fill="white"/>
      ${frame.eyes.map(eye => `<path d="${eye.d}" transform="${eye.matrix}" opacity="${eye.alpha}" fill="black"/>`).join('')}
      ${frame.notch ? `<circle cx="${frame.notch.cx}" cy="${frame.notch.cy}" r="${frame.notch.r}" fill="black"/>` : ''}
    </mask></defs>
    <g fill="none" stroke="#a9c6af" stroke-linecap="round">${arcs('back')}</g>
    ${frame.dotsBehind ? `<g fill="#a9c6af">${dots}</g>` : ''}
    <g opacity="${frame.bodyAlpha}"><rect x="${-vb}" y="${-vb}" width="${vb * 2}" height="${vb * 2}" fill="#a9c6af" mask="url(#body)"/></g>
    ${!frame.dotsBehind ? `<g fill="#a9c6af">${dots}</g>` : ''}
    <g fill="none" stroke="#a9c6af" stroke-linecap="round">${arcs('front')}</g>
  </svg>`;
  frames.push({ input: await sharp(Buffer.from(svg)).png().toBuffer(), left: i % columns * size, top: Math.floor(i / columns) * size });
}
await sharp({ create: { width: columns * size, height: Math.ceil(count / columns) * size, channels: 4, background: '#00000000' } })
  .composite(frames).png().toFile(resolve('assets/studio-bot-startup.png'));
console.log(`Exported ${count} frames at 25 fps from the existing Bloub engine.`);
