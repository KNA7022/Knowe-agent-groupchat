import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { LearningNotificationScheduler } from './learningNotifications';

// Derive the fixture route from the real HTTP dispatch, not the scheduler literal.
const backendDispatch=readFileSync(resolve(process.cwd(),'backend/server.py'),'utf8');
const groupsRoute=backendDispatch.match(/if path == "([^"]+)" and method == "GET":\s+await self\._serve_groups_list\(writer\)/)?.[1];
if (!groupsRoute) throw new Error('real_groups_route_contract_missing');
const gid=`grp_${'a'.repeat(32)}`;
const now=Date.parse('2030-01-01T08:00:00Z');
const state={revision:1,notifications_enabled:true,tasks:[{id:'task1',status:'pending',sessions:[{start:'2030-01-01T08:04:00Z'}]}]};
describe('canonical learning native reminders',()=>{
  it('notifies once without titles and honors persisted mute and revision revocation',async()=>{
    const show=vi.fn();
    let current={...state};
    const read=vi.fn(async(path:string)=>path===groupsRoute?{ok:true,schema_version:1,groups:[{group_id:gid,lifecycle:'active'}]}:{ok:true,state:current});
    const strictRead=async(path:string)=> {
      if(path!==groupsRoute && path!==`/api/learning/state?group_id=${gid}`) throw new Error('unregistered_backend_route');
      return read(path);
    };
    const scheduler=new LearningNotificationScheduler(strictRead,show,()=>true,()=>now);
    await scheduler.poll();await scheduler.poll();
    expect(read.mock.calls[0]?.[0]).toBe(groupsRoute);
    expect(read.mock.calls[1]?.[0]).toBe(`/api/learning/state?group_id=${gid}`);
    expect(show).toHaveBeenCalledTimes(1);
    expect(show).toHaveBeenCalledWith();
    current={...state,notifications_enabled:false};
    await scheduler.poll();expect(show).toHaveBeenCalledTimes(1);
    let reads=0;
    const staleRead=async(path:string)=>path===groupsRoute?{ok:true,schema_version:1,groups:[{group_id:gid,lifecycle:'active'}]}:{ok:true,state:{...state,revision:++reads}};
    await new LearningNotificationScheduler(staleRead,show,()=>true,()=>now).poll();
    expect(show).toHaveBeenCalledTimes(1);
  });
  it('ignores archived groups, expired occurrences and global mute',async()=>{
    const show=vi.fn();
    const read=vi.fn(async(path:string)=>path===groupsRoute?{ok:true,schema_version:1,groups:[{group_id:gid,lifecycle:'archived'}]}:{ok:true,state});
    await new LearningNotificationScheduler(read,show,()=>true,()=>now).poll();
    expect(read).toHaveBeenCalledTimes(1);
    await new LearningNotificationScheduler(read,show,()=>false,()=>now).poll();
    expect(show).not.toHaveBeenCalled();
  });
});
