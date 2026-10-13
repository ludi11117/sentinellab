// Explicit developer dogfood replay. Never submits human votes or signs acceptance.
// Usage: node tests/browser_self_review.cjs <dogfood-report.json> <manual-decisions.json>
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || '../tooling/node_modules/playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
  assert(process.argv[2] && process.argv[3],'Provide a measured report and manually inspected decisions.');
  const report=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
  const decisions=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
  assert.equal(decisions.round_id,report.session.roundId);
  assert.equal(decisions.provenance,'developer_agent_manual_source_inspection');
  const host=process.env.TEST_HOST || 'http://127.0.0.1:8000';
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}}); const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(state=>{if(!sessionStorage.getItem('sentinel-workspace-local'))sessionStorage.setItem('sentinel-workspace-local',JSON.stringify(state));},{...report.session,reviewerKind:'developer_agent',reviewerName:'Codex developer self-test',reviewIndex:0});
    await page.goto(host+'/#review');await page.locator('#vote-form').waitFor();
    for (let i=0;i<report.review.tasks.length;i++) {
      const task=report.review.tasks[i],decision=decisions.items.find(v=>v.item_id===task.item_id);assert(decision);
      // Expand/read the source panel before each submission.
      await page.locator('main details').first().evaluate(el=>el.open=true);
      assert.match(await page.locator('main').innerText(),new RegExp(task.item_id));
      const submit=page.locator('#vote-form button[type=submit]');
      if (!(await submit.isDisabled())) {
        await page.locator('[name=choice][value='+decision.choice+']').check();
        await page.locator('#review-rationale').fill(decision.rationale);
        await submit.click();await page.waitForFunction(()=>document.querySelector('#vote-form button[type=submit]')?.textContent.includes('本题已记录'));
      }
      assert.equal(await page.locator('[name=choice][value='+decision.choice+']').isChecked(),true);
      assert.equal(await page.locator('#review-rationale').inputValue(),decision.rationale);
      assert.equal(await page.locator('[data-action=sign]').isDisabled(),true);
      if(i<report.review.tasks.length-1) await page.locator('[data-action=next-review]:enabled').click();
    }
    const summary=await page.request.get(host+'/api/public/reviews/'+report.session.roundId+'/summary').then(r=>r.json());
    assert.equal(summary.self_reviewed_items,report.review.tasks.length);assert.equal(summary.reviewed_items,0);assert.equal(summary.complete,false);
    const response=await page.request.post(host+'/api/public/acceptances',{data:{gate_id:report.session.gateId,review_round_id:report.session.roundId,notes:'Developer self-test must never satisfy human acceptance'}});
    assert.equal(response.status(),409);
    await page.reload();await page.locator('#vote-form').waitFor();assert.equal(await page.locator('[data-action=sign]').isDisabled(),true);
    await page.evaluate(()=>scrollTo(0,0));if(await page.locator('#notice-dismiss').isVisible())await page.locator('#notice-dismiss').click();
    await page.screenshot({path:path.resolve(__dirname,'../artifacts/workspace-self-review.png'),fullPage:true});
    report.self_review=summary;report.self_review_measured_at=new Date().toISOString();report.browser_checks.self_votes_cannot_sign=true;assert.deepEqual(errors,[]);
    fs.writeFileSync(process.argv[2],JSON.stringify(report,null,2));
    console.log(JSON.stringify({result:'PASS',reviewed:summary.self_reviewed_items,human_coverage:summary.reviewed_items,signoff_rejected:true}));
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
