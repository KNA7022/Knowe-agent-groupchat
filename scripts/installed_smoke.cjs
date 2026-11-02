// Real installed Electron/backend smoke; synthetic local data, no model requests.
const {_electron: electron} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const executablePath = process.argv[2] || path.join(root, '.installation-check', 'StudyCrew 学伴团.exe');
const output = path.join(root, 'evals', 'runs');
const steps = [];
const groupName = '安装恢复验证-合成';
async function launch() {
  const app = await electron.launch({executablePath, cwd:path.dirname(executablePath), timeout:90000});
  const page = await app.firstWindow();
  await page.getByRole('button', {name:'今日', exact:true}).waitFor({timeout:90000});
  const local = page.getByRole('button', {name:'先使用本地资料', exact:true});
  if (await local.isVisible()) await local.click();
  await page.getByRole('button', {name:'今日', exact:true}).click();
  await page.getByLabel('学习群组').getByText(groupName, {exact:true}).waitFor({state:'attached', timeout:90000});
  await page.getByLabel('学习群组').selectOption({label:groupName});
  await page.getByLabel('当前课程').getByText('安装包离散数学', {exact:true}).waitFor({state:'attached', timeout:30000});
  return {app, page};
}
function localTime(date) {
  return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}T${String(date.getHours()).padStart(2,'0')}:${String(date.getMinutes()).padStart(2,'0')}`;
}
(async () => {
  fs.mkdirSync(output, {recursive:true});
  let {app, page} = await launch();
  try {
    steps.push('upgraded_installed_app_restores_existing_course');
    const title = '安装后日历导出验证';
    const displayedTitle = '备考：' + title;
    if (!(await page.getByText(displayedTitle, {exact:true}).count())) {
      const start = new Date(); start.setDate(start.getDate()+2); start.setHours(9,0,0,0);
      const end = new Date(start.getTime()+2*3600000);
      const deadline = new Date(start.getTime()+24*3600000);
      await page.getByLabel('开始', {exact:true}).fill(localTime(start));
      await page.getByLabel('结束', {exact:true}).fill(localTime(end));
      await page.getByRole('button', {name:'添加时段并重排', exact:true}).click();
      await page.getByLabel('任务名称', {exact:true}).fill(title);
      await page.getByLabel('截止时间', {exact:true}).fill(localTime(deadline));
      await page.getByRole('button', {name:'生成计划', exact:true}).click();
      await page.getByText(displayedTitle, {exact:true}).first().waitFor({timeout:30000});
    }
    steps.push('installed_ui_schedules_synthetic_task');
    const calendarPath = path.join(output,'installed-calendar.ics');
    await app.evaluate(({BrowserWindow}, destination) => {
      globalThis.__studycrewDownloadState = 'pending';
      BrowserWindow.getAllWindows()[0].webContents.session.once('will-download', (_event, item) => {
        item.setSavePath(destination);
        item.once('done', (_doneEvent, state) => { globalThis.__studycrewDownloadState = state; });
      });
    }, calendarPath);
    await page.getByRole('button', {name:'导出日历 ICS', exact:true}).click();
    let downloadState = 'pending';
    for (let attempt=0; attempt<100 && downloadState==='pending'; attempt++) {
      await page.waitForTimeout(200);
      downloadState = await app.evaluate(() => globalThis.__studycrewDownloadState);
    }
    assert.equal(downloadState, 'completed');
    const calendar = fs.readFileSync(calendarPath, 'utf8');
    assert.ok(calendar.includes('BEGIN:VCALENDAR') && calendar.includes('BEGIN:VEVENT') && calendar.includes('UID:'));
    steps.push('installed_ui_downloads_calendar_with_scheduled_event');
    const memoryText = '集合交集表示同时属于两个集合的元素。合成验证编号：' + Date.now();
    await page.locator('article[aria-label="课程记忆"] select').first().selectOption('public');
    await page.getByText('Mem0 语义能力：', {exact:false}).waitFor();
    await page.getByLabel('记忆内容', {exact:true}).fill(memoryText);
    await page.getByLabel('记忆敏感度', {exact:true}).selectOption('public');
    await page.getByRole('button', {name:'保存记忆', exact:true}).click();
    await page.getByRole('button', {name:'确认此版本', exact:true}).click();
    await page.getByLabel('记忆语义查询', {exact:true}).fill('集合的交集是什么？');
    await page.getByRole('button', {name:'Mem0 语义召回', exact:true}).click();
    await page.getByText('Mem0 语义能力：已验证可用', {exact:false}).waitFor({timeout:90000});
    const memoryRow = page.locator('article[aria-label="课程记忆"] .learning-task').filter({hasText:memoryText});
    assert.equal(await memoryRow.getByLabel('编辑记忆内容', {exact:true}).inputValue(), memoryText);
    steps.push('installed_mem0_e5_usearch_semantic_recall_via_ui');
    await memoryRow.getByRole('button', {name:'删除并阻止重新学习', exact:true}).click();
    await memoryRow.waitFor({state:'hidden'});
    await page.getByRole('button', {name:'Mem0 语义召回', exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('article[aria-label="课程记忆"] fieldset').disabled);
    assert.equal(await page.locator('article[aria-label="课程记忆"] [role="alert"]').count(), 0);
    await memoryRow.waitFor({state:'hidden'});
    steps.push('installed_memory_delete_and_no_recall_via_ui');
    await page.screenshot({path:path.join(output,'installed-final.png'), fullPage:true});
  } catch (error) {
    fs.writeFileSync(path.join(output,'installed-failure.txt'), await page.locator('body').innerText(), 'utf8');
    await page.screenshot({path:path.join(output,'installed-failure.png'), fullPage:true});
    throw error;
  } finally { await app.close(); }
  ({app, page} = await launch());
  try {
    await page.getByText('备考：安装后日历导出验证', {exact:true}).first().waitFor({timeout:30000});
    steps.push('installed_restart_restores_new_task');
  } finally { await app.close(); }
  fs.writeFileSync(path.join(output,'installed-final.json'), JSON.stringify({passed:true, steps, scope:'actual installed Electron and frozen backend; no model request'},null,2)+'\n','utf8');
  console.log(JSON.stringify({passed:true,steps}));
})().catch(error => {console.error(error.name+': '+error.message); console.log(JSON.stringify({passed:false,steps})); process.exitCode=1;});
