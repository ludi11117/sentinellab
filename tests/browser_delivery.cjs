// Backwards-compatible entry point for the unified workspace.
require('./browser_workspace.cjs')('quick').catch(error=>{console.error(error);process.exitCode=1;});
