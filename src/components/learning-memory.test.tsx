// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MemoryPanel } from './MemoryPanel';

vi.mock('../shared/runtimeFetch', () => ({runtimeFetch: (...args: unknown[]) => fetch(...args as [RequestInfo, RequestInit])}));
vi.mock('../shared/runtimeEndpoints', () => ({runtimeHttpBase: () => 'http://localhost:18999'}));
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
const candidate = {id:'memory-a',text:'Synthetic preference',version:1,status:'candidate',sensitivity:'personal',expires_at:null,source:{kind:'manual',version:1}};
function response(records: unknown[], candidates: unknown[]) {return {ok:true,json:async()=>({ok:true,records,candidates})} as Response;}

it('reviews a version-bound candidate through the actual memory action contract', async()=>{
  const calls: Array<{url:string;payload:Record<string,unknown>}> = [];
  vi.stubGlobal('fetch', vi.fn(async(input:string,init?:RequestInit)=>{
    const payload=JSON.parse(String(init?.body || '{}'));calls.push({url:input,payload});
    if(payload.action==='approve')return response([{...candidate,status:'active',version:2}],[]);
    if(payload.action==='delete')return response([],[]);
    return response([],[candidate]);
  }));
  render(<MemoryPanel groupId="g1" courseId="c1"/>);
  fireEvent.click(await screen.findByRole('button',{name:'确认此版本'}));
  await waitFor(()=>expect(calls.some(c=>c.payload.action==='approve')).toBe(true));
  const approval=calls.find(c=>c.payload.action==='approve')!;
  expect(new URL(approval.url).searchParams.get('group_id')).toBe('g1');
  expect(new URL(approval.url).searchParams.get('course_id')).toBe('c1');
  expect(approval.payload).toMatchObject({memory_id:'memory-a',version:1,confirmed:true});
  await screen.findByText(/版本 2/);
  fireEvent.click(screen.getByRole('button',{name:'删除并阻止重新学习'}));
  await screen.findByText('该课程和敏感度范围暂无记忆。');
  expect(calls.find(c=>c.payload.action==='delete')!.payload.version).toBe(2);
});

it('discards late private detail after the course changes', async()=>{
  let resolve!: (value: Response) => void;
  vi.stubGlobal('fetch',vi.fn((input:string)=>input.includes('course_id=c1')?new Promise<Response>(done=>{resolve=done;}):Promise.resolve(response([],[]))));
  const view=render(<MemoryPanel groupId="g1" courseId="c1"/>);
  view.rerender(<MemoryPanel groupId="g2" courseId="c2"/>);
  await screen.findByText('该课程和敏感度范围暂无记忆。');
  resolve(response([],[candidate]));
  await waitFor(()=>expect(screen.queryByText(/Synthetic preference/)).not.toBeInTheDocument());
});
