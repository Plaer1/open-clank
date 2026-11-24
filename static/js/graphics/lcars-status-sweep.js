// Pure Status Sweep scene. The theme owner supplies the canvas, clock and lifecycle.
const clamp = (value, min, max, fallback) => {
  const number = Number(value);
  return Math.min(max, Math.max(min, Number.isFinite(number) ? number : fallback));
};

export function createLcarsStatusSweep() {
  const state = { phase:0, lastTime:null };
  return {
    state,
    draw(ctx, { width, height, time, reduced, intensity = 1, size = 1, colors = [], controls = {} }) {
      const speed = clamp(controls.scanSpeed, 0, 300, 100) / 100;
      const clock = Number.isFinite(Number(time)) ? Number(time) : 0;
      if (reduced) state.lastTime = null;
      else {
        const elapsed = state.lastTime == null ? 0 : Math.min(120, Math.max(0, clock - state.lastTime));
        state.lastTime = clock;
        state.phase = (state.phase + elapsed * speed / 22000) % 1;
      }
      const phase = reduced ? .38 : state.phase;
      const style = Math.round(clamp(controls.sweepStyle, 0, 3, 0));
      const reverse = controls.statusSweepDirection === 'rtl';
      const range = clamp(controls.scanRange, 20, 100, 80) / 100;
      const beam = clamp(controls.beamWidth, 5, 80, 20) / 100;
      const brightness = clamp(controls.sweepBrightness, 0, 100, 45) / 100;
      const detail = clamp(controls.gridDetail, 0, 100, 35) / 100;
      const mode = Math.round(clamp(controls.mode, 0, 3, 0));
      const palette = [0, 1, 2, 3].map(index => colors[(index + mode) % Math.max(1, colors.length)] || '#62C7E8');
      const alpha = clamp(intensity, 0, 1, 1);
      const grid = Math.max(12, 32 * clamp(size, .25, 4, 1));
      ctx.save();
      ctx.strokeStyle = palette[0]; ctx.lineWidth = 1; ctx.globalAlpha = alpha * detail * .12;
      ctx.beginPath();
      for (let x = 0; x <= width; x += grid) { ctx.moveTo(x, 0); ctx.lineTo(x, height); }
      for (let y = 0; y <= height; y += grid) { ctx.moveTo(0, y); ctx.lineTo(width, y); }
      ctx.stroke();
      const travel = style === 1 ? (phase < .5 ? phase * 2 : (1 - phase) * 2) : phase;
      const directed = reverse ? 1 - travel : travel;
      const center = .5 + (directed - .5) * range * 1.8;
      const drawBand = (position, diagonal = false) => {
        const bandWidth = Math.max(1, width * beam);
        const x = position * width;
        ctx.save();
        if (diagonal) { ctx.translate(width / 2, height / 2); ctx.rotate(-.35); ctx.translate(-width / 2, -height / 2); }
        const gradient = ctx.createLinearGradient(x - bandWidth, 0, x + bandWidth, 0);
        gradient.addColorStop(0, 'transparent');
        gradient.addColorStop(.3, palette[0]); gradient.addColorStop(.48, palette[1]);
        gradient.addColorStop(.62, palette[2]); gradient.addColorStop(.75, palette[3]);
        gradient.addColorStop(1, 'transparent');
        ctx.fillStyle = gradient; ctx.globalAlpha = alpha * brightness * .65;
        ctx.fillRect(x - bandWidth, diagonal ? -height : 0, bandWidth * 2, diagonal ? height * 3 : height);
        ctx.restore();
      };
      drawBand(center, style === 3);
      if (style === 2) drawBand(1 - center);
      ctx.restore();
    },
  };
}
