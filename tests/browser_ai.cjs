// Backwards-compatible entry point for the unified workspace.
require('./browser_workspace.cjs')('model').catch(error=>{console.error(error);process.exitCode=1;});
