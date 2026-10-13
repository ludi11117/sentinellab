// Real forms and refresh, with no human vote submission.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || '../tooling/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
  const root=path.resolve(__dirname,'..'),host=process.env.TEST_HOST || 'http://127.0.0.1:8000';
  const report=JSON.parse(fs.readFileSync(path.join(root,'artifacts/dogfood-quick.json'),'utf8'));
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(state=>{if(!sessionStorage.getItem('sentinel-workspace-local'))sessionStorage.setItem('sentinel-workspace-local',JSON.stringify(state));},{...report.session,reviewerKind:'human',reviewIndex:0});
    await page.goto(host+'/#review');await page.locator('#vote-form').waitFor();
    await page.locator('#reviewer-name').fill('Browser draft, no vote');
    await page.locator('[name=choice][value=tie]').check();
    await page.locator('#review-rationale').fill('浏览器草稿测试：切换焦点与刷新不应清空此段文字，不提交人工评审。');
    await page.locator('#reviewer-name').click();
    assert.equal(await page.locator('[name=choice][value=tie]').isChecked(),true);
    await page.reload();await page.locator('#vote-form').waitFor();
    assert.equal(await page.locator('[name=choice][value=tie]').isChecked(),true);
    assert.match(await page.locator('#review-rationale').inputValue(),/切换焦点与刷新不应清空/);
    assert.equal(await page.locator('#reviewer-name').inputValue(),'Browser draft, no vote');
    await page.locator('[data-action=next-review]').click();await page.locator('[data-action=prev-review]').click();
    assert.match(await page.locator('#review-rationale').inputValue(),/切换焦点与刷新不应清空/);
    const summary=await page.request.get(host+'/api/public/reviews/'+report.session.roundId+'/summary').then(r=>r.json());assert.equal(summary.votes,0);
    await page.goto(host+'/#setup');await page.locator('[data-action=template][data-id=custom]').click();
    await page.locator('#bundle-file').setInputFiles({name:'invalid.json',mimeType:'application/json',buffer:Buffer.from('{broken')});
    await page.locator('#notice').filter({hasText:'不是有效的 JSON'}).waitFor();
    await page.locator('#bundle-file').setInputFiles(path.join(root,'frontend/demo-bundle.json'));await page.locator('[data-action=import]:enabled').waitFor();
    assert.match(await page.locator('main').innerText(),/36 道题/);
    await page.goto(host+'/#compare');await page.locator('#case-filter').waitFor();
    await page.locator('#notice-dismiss').click();
    await page.evaluate(()=>{scrollTo(0,0);});
    await page.screenshot({path:path.join(root,'artifacts/workspace-quick-compare.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);console.log('PASS: radios/textarea retain values, refresh and per-item drafts survive, invalid JSON recovery, custom file preview; zero human votes.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
