// Observe the real installed scheduler and native Notification.show; no model call.
const {_electron:electron} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname,'..');
const output = path.join(root,'evals','runs','installed-notification.json');
const dateText = date => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}T${String(date.getHours()).padStart(2,'0')}:${String(date.getMinutes()).padStart(2,'0')}`;
(async()=>{
  const app = await electron.launch({executablePath:path.join(root,'.installation-check','StudyCrew 学伴团.exe'),timeout:90000});
  const report = {passed:false,scope:'installed UI schedule and actual native notification event; no mocks or model calls'};
  let page, changed=false;
  try {
    page=await app.firstWindow();
    await page.getByRole('button',{name:'今日',exact:true}).waitFor({timeout:90000});
    const local=page.getByRole('button',{name:'先使用本地资料',exact:true});
    if(await local.isVisible())await local.click();
    await page.getByRole('button',{name:'今日',exact:true}).click({timeout:90000});
    await page.getByLabel('学习群组').getByText('安装恢复验证-合成',{exact:true}).waitFor({state:'attached',timeout:90000});
    await page.getByLabel('学习群组').selectOption({label:'安装恢复验证-合成'});
    await page.getByLabel('当前课程').getByText('安装包离散数学',{exact:true}).waitFor({state:'attached',timeout:30000});
    report.supported=await app.evaluate(({Notification})=>{
      globalThis.__studycrewNotice={calls:0,shown:0,failed:0};
      const original=Notification.prototype.show;
      Notification.prototype.show=function(){
        globalThis.__studycrewNotice.calls++;
        this.once('show',()=>globalThis.__studycrewNotice.shown++);
        this.once('failed',()=>globalThis.__studycrewNotice.failed++);
        return original.call(this);
      };
      return Notification.isSupported();
    });
    if (!report.supported) throw new Error('native_notification_unsupported');
    const start=new Date(Date.now()+120000);start.setSeconds(0,0);
    const end=new Date(start.getTime()+600000), deadline=new Date(end.getTime()+600000);
    await page.getByLabel('开始',{exact:true}).fill(dateText(start));
    await page.getByLabel('结束',{exact:true}).fill(dateText(end));
    await page.getByRole('button',{name:'添加时段并重排',exact:true}).click();
    await page.getByLabel('任务名称',{exact:true}).fill('原生通知合成验证');
    await page.getByLabel('截止时间',{exact:true}).fill(dateText(deadline));
    await page.getByLabel('预计分钟',{exact:true}).fill('5');
    await page.getByRole('button',{name:'生成计划',exact:true}).click();
    await page.getByText('备考：原生通知合成验证',{exact:true}).first().waitFor();
    const enable=page.getByRole('button',{name:'开启系统提醒',exact:true});
    if(await enable.isVisible()){await enable.click();changed=true;}
    await page.getByRole('button',{name:'关闭系统提醒',exact:true}).waitFor();
    for(let attempt=0;attempt<35;attempt++){
      report.native=await app.evaluate(()=>globalThis.__studycrewNotice);
      if(report.native.shown || report.native.failed)break;
      await page.waitForTimeout(2000);
    }
    report.passed=report.native.calls>0 && report.native.shown>0 && report.native.failed===0;
  }catch(error){report.error=error.name+': '+error.message;}
  finally{
    if(changed && page){const disable=page.getByRole('button',{name:'关闭系统提醒',exact:true});if(await disable.isVisible()){await disable.click();await page.getByRole('button',{name:'开启系统提醒',exact:true}).waitFor();}}
    await app.close();
    fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n','utf8');
    console.log(JSON.stringify(report));
  }
  if(!report.passed)process.exitCode=1;
})().catch(error=>{console.error(error.name+': '+error.message);process.exitCode=1;});
