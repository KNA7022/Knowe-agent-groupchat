import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const css = readFileSync(resolve(process.cwd(), 'src/styles/knowe-components.css'), 'utf8');

describe('StudyCrew responsive and motion styles', () => {
  it('disables non-essential motion when the operating system requests it', () => {
    expect(css).toContain('@media (prefers-reduced-motion:reduce)');
    expect(css).toMatch(/\.agent-status \*,\.runtime-drawer \*[^}]*animation\s*:\s*none!important/s);
    expect(css).toMatch(/\.agent-status \*,\.runtime-drawer \*[^}]*transition\s*:\s*none!important/s);
  });

  it('virtualizes the public runtime event list and collapses the conversation rail at 900px', () => {
    expect(css).toMatch(/\.runtime-event-list li\s*\{[^}]*content-visibility\s*:\s*auto/s);
    expect(css).toMatch(/@media \(max-width:900px\)[\s\S]*\.clist,\.side\s*\{\s*width\s*:\s*76px/s);
  });
});
