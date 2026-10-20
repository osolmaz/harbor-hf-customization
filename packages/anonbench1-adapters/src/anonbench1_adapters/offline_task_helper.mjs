// Runs inside the Harbor task. Only public Pi tool factories execute task I/O.
import { readFile, readdir, rename, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const [root, cwd, packageEntry] = process.argv.slice(2);
if (process.version !== 'v26.11.1') throw new Error('Node pin mismatch');
const pi = await import(pathToFileURL(packageEntry).href);
if (pi.VERSION !== '1.3.0') throw new Error('Pi pin mismatch');
const factories = [pi.createReadToolDefinition, pi.createWriteToolDefinition, pi.createEditToolDefinition,
  pi.createGrepToolDefinition, pi.createFindToolDefinition, pi.createLsToolDefinition, pi.createBashToolDefinition];
const tools = new Map(factories.map(factory => {
  const tool = factory(cwd);
  return [tool.name, tool];
}));
const pending = new Map();
const accepted = new Set();
let closing = false;
let resources;

// Resource discovery and skill expansion read task paths only in this process.
async function control(name, args) {
  if (name === 'harbor_resources') {
    const settingsManager = pi.SettingsManager.inMemory();
    settingsManager.setProjectTrusted(true);
    const loader = new pi.DefaultResourceLoader({
      cwd, agentDir: `${root}/config`, settingsManager,
      noExtensions: true, noThemes: true,
      additionalSkillPaths: args.skillsDir ? [args.skillsDir] : [],
    });
    await loader.reload();
    resources = {
      agents: loader.getAgentsFiles(), skills: loader.getSkills(), prompts: loader.getPrompts(),
      systemPrompt: loader.getSystemPrompt(), appendSystemPrompt: loader.getAppendSystemPrompt(),
    };
    return resources;
  }
  if (name === 'harbor_expand_skill') {
    if (!resources) throw new Error('Task resources have not been loaded');
    const match = /^\/skill:([^\s]+)(?:\s+([\s\S]*))?$/.exec(args.text);
    const skill = match && resources.skills.skills.find(item => item.name === match[1]);
    if (!skill) return args.text;
    const body = pi.stripFrontmatter(await readFile(skill.filePath, 'utf8')).trim();
    const block = `<skill name="${skill.name}" location="${skill.filePath}">\nReferences are relative to ${skill.baseDir}.\n\n${body}\n</skill>`;
    return match[2]?.trim() ? `${block}\n\n${match[2].trim()}` : block;
  }
  throw new Error('Invalid control request');
}

async function publish(name, value) {
  const path = join(root, name);
  await writeFile(`${path}.tmp`, JSON.stringify(value), { mode: 0o600 });
  await rename(`${path}.tmp`, path);
}

async function execute(id) {
  const controller = new AbortController();
  const record = { controller, promise: undefined };
  pending.set(id, record);
  record.promise = (async () => {
    let updates = Promise.resolve();
    let sequence = 0;
    try {
      const request = JSON.parse(await readFile(join(root, `${id}.request`), 'utf8'));
      const tool = tools.get(request.tool);
      if (typeof request.callId !== 'string') throw new Error('Invalid tool request');
      const metadata = request.context;
      const ctx = metadata ? {
        cwd, model: metadata.model, thinkingLevel: metadata.thinkingLevel,
        sessionManager: {
          getSessionId: () => metadata.sessionId,
          getSessionFile: () => metadata.sessionFile,
        },
      } : undefined;
      const result = tool ? await tool.execute(request.callId, request.args, controller.signal, update => {
        const index = sequence++;
        updates = updates.then(() => publish(`${id}.update.${index}`, update));
      }, ctx) : await control(request.tool, request.args);
      await updates;
      await publish(`${id}.result`, { ok: true, result, updates: sequence });
    } catch (error) {
      await updates;
      await publish(`${id}.result`, { ok: false, error: String(error.message ?? error), updates: sequence });
    } finally {
      pending.delete(id);
    }
  })();
  // A publication failure is fatal: the owner must not infer settlement from silence.
  record.promise.catch(error => { console.error(error); process.exitCode = 1; closing = true; });
}

await publish('ready', { node: process.version, pi: pi.VERSION, pid: process.pid, tools: [...tools.keys()] });
while (!closing) {
  const files = await readdir(root);
  closing = files.includes('stop');
  for (const file of files) {
    const match = /^([a-f0-9]{32})\.request$/.exec(file);
    if (!closing && match && !accepted.has(match[1])) {
      accepted.add(match[1]);
      await execute(match[1]);
    }
  }
  for (const [id, record] of pending) {
    if (closing || files.includes(`${id}.cancel`)) record.controller.abort();
  }
  if (!closing) await delay(25);
}
await Promise.all([...pending.values()].map(record => record.promise));
if (!process.exitCode) await publish('closed', { settled: true, calls: accepted.size });
