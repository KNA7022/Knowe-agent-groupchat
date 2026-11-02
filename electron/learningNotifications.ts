/** Canonical polling scheduler; renderer text never reaches OS notifications. */
export class LearningNotificationScheduler {
  private claimed = new Map<string, number>();
  private running = false;
  constructor(private readonly read: (path: string) => Promise<unknown>,
    private readonly show: () => void, private readonly allowed: () => boolean,
    private readonly now: () => number = Date.now) {}
  async poll(): Promise<void> {
    if (this.running || !this.allowed()) return;
    this.running = true;
    try {
      const snapshot = await this.read('/groups') as { ok?: boolean; groups?: Array<{ group_id: string; lifecycle: string }> };
      if (!snapshot.ok || !Array.isArray(snapshot.groups)) return;
      for (const group of snapshot.groups.slice(0, 100)) {
        if (group.lifecycle !== 'active' || !/^grp_[a-f0-9]{32}$/.test(group.group_id)) continue;
        const path = `/api/learning/state?group_id=${encodeURIComponent(group.group_id)}`;
        const result = await this.read(path) as { ok?: boolean; state?: { revision: number; notifications_enabled?: boolean; tasks?: Array<{id:string;status:string;sessions:Array<{start:string}>}> } };
        const state = result.state;
        if (!result.ok || !state?.notifications_enabled || !Array.isArray(state.tasks)) continue;
        for (const task of state.tasks) {
          if (task.status !== 'pending' || !Array.isArray(task.sessions)) continue;
          for (const session of task.sessions) {
            const start = Date.parse(session.start), delta = start - this.now();
            const key = `${group.group_id}:${task.id}:${session.start}`;
            if (!Number.isFinite(start) || delta > 300000 || delta <= -60000 || this.claimed.has(key)) continue;
            // Re-resolve immediately before OS delivery; stale mutations revoke the notice.
            const current = await this.read(path) as typeof result;
            if (!current.ok || !current.state?.notifications_enabled || current.state.revision !== state.revision || !this.allowed()) break;
            this.claimed.set(key, start);
            this.show();
          }
        }
      }
      for (const [key, start] of this.claimed) if (start < this.now() - 86400000) this.claimed.delete(key);
    } catch { /* Backend unavailable: no fabricated notification and retry next poll. */ }
    finally { this.running = false; }
  }
}
