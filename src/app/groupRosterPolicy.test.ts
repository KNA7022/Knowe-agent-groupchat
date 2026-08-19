import { describe, expect, it } from 'vitest';

import { shouldCloseLegacyRoster, shouldMountLegacyRoster } from './App';

describe('canonical Group roster boundary', () => {
  it('closes and unmounts legacy roster on legacy-to-canonical navigation', () => {
    expect(shouldMountLegacyRoster(false, null)).toBe(true);
    expect(shouldCloseLegacyRoster(false, null)).toBe(false);
    expect(shouldCloseLegacyRoster(false, `grp_${'1'.repeat(32)}`)).toBe(true);
    expect(shouldMountLegacyRoster(false, `grp_${'1'.repeat(32)}`)).toBe(false);
  });

  it('keeps legacy roster absent across canonical Group-to-Group navigation', () => {
    expect(shouldMountLegacyRoster(false, `grp_${'1'.repeat(32)}`)).toBe(false);
    expect(shouldMountLegacyRoster(false, `grp_${'2'.repeat(32)}`)).toBe(false);
  });
});
