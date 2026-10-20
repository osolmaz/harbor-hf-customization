// The SDK registry keeps Pi's definitions; execution crosses the Harbor owner.
// Constructing these definitions performs no task-path I/O. Never invoke their
// original execute methods on the host, including from nested codemode calls.
export function createHarborTaskTools(pi, execute, cwd = process.cwd()) {
  const factories = [pi.createReadToolDefinition, pi.createWriteToolDefinition,
    pi.createEditToolDefinition, pi.createGrepToolDefinition, pi.createFindToolDefinition,
    pi.createLsToolDefinition, pi.createBashToolDefinition];
  return factories.map(factory => {
    const definition = factory(cwd);
    return {
      ...definition,
      execute: (id, args, signal, onUpdate, ctx) => execute(definition.name, id, args, signal, onUpdate,
        ctx ? {
          sessionId: ctx.sessionManager.getSessionId(),
          sessionFile: ctx.sessionManager.getSessionFile(),
          thinkingLevel: ctx.thinkingLevel,
          model: ctx.model ? {
            id: ctx.model.id, provider: ctx.model.provider, input: ctx.model.input,
            inputLimits: ctx.model.inputLimits,
          } : undefined,
        } : undefined),
    };
  });
}

// Host-only IPC with the Python adapter, not a container stdin API or provider relay.
export function createHarborToolClient(input, output) {
  let sequence = 0;
  let buffer = '';
  let failure;
  const pending = new Map();
  const unsettled = new Set();
  const send = message => output.write(`${JSON.stringify(message)}\n`);
  const fail = error => {
    failure ??= error;
    for (const entry of pending.values()) { entry.dispose(); entry.reject(failure); }
    pending.clear();
  };
  input.setEncoding('utf8');
  input.on('end', () => fail(new Error('Harbor tool owner disconnected')));
  input.on('error', fail);
  input.on('data', chunk => {
    buffer += chunk;
    let newline;
    while ((newline = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      let message;
      try { message = JSON.parse(line); } catch { fail(new Error('Invalid Harbor response')); return; }
      const entry = pending.get(message.id);
      if (!entry) { fail(new Error('Unexpected Harbor response')); return; }
      if (message.type === 'update') { entry.onUpdate?.(message.result); continue; }
      pending.delete(message.id);
      entry.dispose();
      if (message.ok) entry.resolve(message.result);
      else entry.reject(new Error(message.error));
    }
  });
  const execute = (tool, callId, args, signal, onUpdate, context) => {
    const promise = new Promise((resolve, reject) => {
      if (failure) { reject(failure); return; }
      if (signal?.aborted) { reject(new Error('aborted')); return; }
      const id = String(++sequence);
      const abort = () => send({ type: 'cancel', id });
      pending.set(id, { resolve, reject, onUpdate, dispose: () => signal?.removeEventListener('abort', abort) });
      signal?.addEventListener('abort', abort, { once: true });
      send({ type: 'call', id, tool, callId, args, context });
    });
    unsettled.add(promise);
    promise.then(() => unsettled.delete(promise), () => unsettled.delete(promise));
    return promise;
  };
  // Codemode aborts nested calls without awaiting their completion. Its owner
  // must drain these acknowledged remote settlements before ending a turn.
  execute.settle = async () => {
    while (unsettled.size) await Promise.allSettled([...unsettled]);
    if (failure) throw failure;
  };
  return execute;
}
