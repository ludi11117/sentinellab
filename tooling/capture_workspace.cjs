const {chromium}=require(process.env.PLAYWRIGHT_MODULE || './node_modules/playwright');
const fs=require('node:fs'),path=require('node:path');
(async()=>{
  const root=path.resolve(__dirname,'..'),host=process.env.TEST_HOST || 'http://127.0.0.1:8000';
  const report=JSON.parse(fs.readFileSync(path.join(root,'artifacts/dogfood-model.json'),'utf8'));
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    await page.addInitScript(state=>{if(!sessionStorage.getItem('sentinel-workspace-local'))sessionStorage.setItem('sentinel-workspace-local',JSON.stringify(state));},{...report.session,reviewerKind:'developer_agent',reviewIndex:6});
    async function capture(name,fullPage=false) {await page.evaluate(()=>scrollTo(0,0));await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));await page.screenshot({path:path.join(root,'artifacts',name),fullPage});}
    await page.goto(host+'/#compare');await page.locator('#case-filter').waitFor();await capture('workspace-model-compare.png');
    await page.locator('#case-filter').selectOption('all');await page.locator('[data-action=case-detail][data-id=holdout-06]').click();await page.locator('#detail-dialog[open]').waitFor();
    await capture('workspace-model-fix.png');await page.locator('#detail-dialog [data-close]').click();
    await page.goto(host+'/#review');await page.locator('#vote-form').waitFor();await capture('workspace-model-review.png');
    await page.setViewportSize({width:390,height:844});await capture('workspace-model-mobile.png',true);
    await page.setViewportSize({width:1440,height:1000});await page.goto(host+'/#setup');await page.locator('.template').first().waitFor();await page.locator('[data-action=template][data-id=ops]').click();await page.locator('[data-action=import]:enabled').waitFor();await capture('workspace-setup.png');
    console.log('PASS: refreshed measured-results, failure-fix, self-review and template screenshots; no new jobs or votes.');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
