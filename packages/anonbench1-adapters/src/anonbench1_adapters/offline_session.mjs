// The host owns Pi's model connection and native session. All task I/O is remote.
import { appendFileSync, mkdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import campaignEvidence from './anonbench1_evidence.mjs';
import { createHarborTaskTools, createHarborToolClient } from './offline_tools.mjs';

export async function createOfflineSession(config, remote) {
  if (process.version !== 'v26.11.1') throw new Error('Node pin mismatch');
  const pi = await import(pathToFileURL(config.entry).href);
  if (pi.VERSION !== '1.3.0') throw new Error('Pi pin mismatch');
  const resources = await remote('harbor_resources', 'resources', { skillsDir: config.skillsDir });
  const settingsManager = pi.SettingsManager.inMemory();
  let sessionFailure;
  const factories = [campaignEvidence];
  if (config.codeMode) factories.push(pi.createCodemodeExtension());
  factories.push(api => {
    // The public input hook runs before Pi's host-local /skill expansion.
    api.on('input', async event => {
      if (!event.text.startsWith('/skill:')) return { action: 'continue' };
      try {
        const text = await remote('harbor_expand_skill', 'skill', { text: event.text });
        return { action: 'transform', text, images: event.images };
      } catch (error) {
        // Pi catches extension errors and continues input handling. Explicitly
        // consume failed expansion so it cannot fall back to a host file read.
        sessionFailure = error;
        return { action: 'handled' };
      }
    });
    if (config.maxTurns != null) {
      let turns = 0;
      api.on('before_agent_start', event => ({
        systemPrompt: `${event.systemPrompt}\n\nYou have a hard budget of ${config.maxTurns} model turns. Complete the task and provide your final answer within that budget.`,
      }));
      api.on('turn_end', (_event, ctx) => { if (++turns >= config.maxTurns) ctx.abort(); });
    }
  });
  const loader = new pi.DefaultResourceLoader({
    cwd: config.agentDir, agentDir: config.agentDir, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
    noContextFiles: true, extensionFactories: factories,
    agentsFilesOverride: () => resources.agents,
    skillsOverride: () => resources.skills,
    promptsOverride: () => resources.prompts,
    systemPromptOverride: () => resources.systemPrompt,
    appendSystemPromptOverride: () => resources.appendSystemPrompt,
  });
  await loader.reload();
  const modelRuntime = await pi.ModelRuntime.create({
    authPath: `${config.agentDir}/auth.json`, modelsPath: `${config.agentDir}/models.json`,
    allowModelNetwork: false, refreshOnCreate: false,
  });
  const model = modelRuntime.getModel(config.provider, config.model);
  if (!model) throw new Error('Requested Pi model is absent from the pinned local configuration');
  mkdirSync(config.sessionDir, { recursive: true });
  const sessionManager = config.resume
    ? pi.SessionManager.continueRecent(config.cwd, config.sessionDir)
    : pi.SessionManager.create(config.cwd, config.sessionDir);
  const result = await pi.createAgentSession({
    cwd: config.cwd, agentDir: config.agentDir, settingsManager, resourceLoader: loader,
    modelRuntime, model, thinkingLevel: config.thinking ?? undefined, sessionManager,
    customTools: createHarborTaskTools(pi, remote, config.cwd),
    tools: ['read', 'bash', 'edit', 'write', ...(config.codeMode ? ['codemode'] : [])],
  });
  // The public Agent loop awaits finishTurn before another request. Preserve
  // Pi's installed hook, but end explicitly on a fatal transport failure:
  // the provider's abort signal alone does not prevent another stream callback.
  const finishTurn = result.session.agent.finishTurn;
  result.session.agent.finishTurn = async (turn, signal) => {
    try { await remote.settle(); } catch (error) { sessionFailure = error; }
    const decision = await finishTurn?.(turn, signal);
    return sessionFailure ? { action: 'end' } : decision;
  };
  // Preserve the CLI's JSON event log alongside Pi's own session JSONL.
  result.session.subscribe(event => {
    if (event.type !== 'message_update') appendFileSync(config.output, `${JSON.stringify(event)}\n`);
  });
  await result.session.bindExtensions({ mode: 'json' });
  return { session: result.session, checkInput: () => { if (sessionFailure) throw sessionFailure; } };
}

export async function runOfflineSession({ session, checkInput }, instruction, remote) {
  const abort = () => { void session.abort(); };
  process.on('SIGTERM', abort);
  try {
    checkInput();
    await session.prompt(instruction);
    checkInput();
    const last = session.state.messages.at(-1);
    if (last?.role === 'assistant' && ['error', 'aborted'].includes(last.stopReason)) {
      throw new Error(`Pi session ended with ${last.stopReason}; inspect its native session log`);
    }
  } finally {
    try {
      await session.abort();
      await remote.settle();
    } finally {
      session.dispose();
      process.off('SIGTERM', abort);
    }
  }
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  const remote = createHarborToolClient(process.stdin, process.stdout);
  try {
    const session = await createOfflineSession(config, remote);
    await runOfflineSession(session, config.instruction, remote);
  } finally {
    process.stdin.destroy();
  }
}
