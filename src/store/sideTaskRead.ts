import { create } from 'zustand';

const STORAGE_KEY = 'studycrew.side-task-read-watermarks.v1';

function loadWatermarks(): Record<string, number> {
  if (typeof localStorage === 'undefined') return {};
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') as Record<string, unknown>;
    return Object.fromEntries(Object.entries(value).filter(([, seq]) => Number.isInteger(seq) && Number(seq) >= 0)) as Record<string, number>;
  } catch { return {}; }
}

interface SideTaskReadState {
  watermarks: Record<string, number>;
  acknowledge: (runId: string, seq: number) => void;
  reset: () => void;
}

export const useSideTaskReadStore = create<SideTaskReadState>((set) => ({
  watermarks: loadWatermarks(),
  acknowledge(runId, seq): void {
    set((state) => {
      const watermarks = { ...state.watermarks, [runId]: Math.max(seq, state.watermarks[runId] || 0) };
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(watermarks)); } catch { /* local-only best effort */ }
      return { watermarks };
    });
  },
  reset(): void {
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* local-only best effort */ }
    set({ watermarks: {} });
  },
}));
