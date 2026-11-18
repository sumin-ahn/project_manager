"""opencode 2.x 가드 플러그인 시험 공용 — 플러그인 setup 이 받는 컨텍스트 대역과 훅·이벤트 모양.

모양은 opencode 2.3.0 실측이다: tool `execute.before` 이벤트 { tool, sessionID, agent, messageID, id, input },
session `prompt` 이벤트 { sessionID, messageID, prompt: { text }, delivery }, session `context` 이벤트
{ sessionID, agent, system: [{ type, text }] }, 버스 이벤트 { type, data: { sessionID, ... } }, 세션 조회
session.get({ sessionID }) -> Session(자식이면 parentID). node 스크립트 앞에 `FAKE_CONTEXT_JS` 를 붙여 쓴다.
"""

FAKE_CONTEXT_JS = r'''
function fakeContext({directory = process.cwd(), sessions = {}, get = null} = {}) {
  const hooks = {tool: {}, session: {}};
  const subscriptions = [];
  const ctx = {
    location: {directory},
    session: {
      get: get || (async ({sessionID}) => ({id: sessionID, ...(sessions[sessionID] || {})})),
      hook: async (name, callback) => { hooks.session[name] = callback; return {dispose: async () => {}}; },
    },
    tool: {hook: async (name, callback) => { hooks.tool[name] = callback; return {dispose: async () => {}}; }},
    event: {subscribe: (options) => {
      subscriptions.push(options);
      return {[Symbol.asyncIterator]: () => ({next: () => new Promise(() => {})})};
    }},
  };
  return {ctx, hooks, subscriptions};
}
async function setupPlugin(plugin, options) {
  const fake = fakeContext(options);
  await plugin.setup(fake.ctx);
  return fake.hooks;
}
const toolEvent = (tool, sessionID, input) => ({tool, sessionID, agent: "build", messageID: "msg", id: "call", input});
const promptEvent = (sessionID, text) => ({sessionID, messageID: "msg", prompt: {text}, delivery: "steer"});
const contextEvent = (sessionID) => ({sessionID, agent: "build", system: []});
const systemTexts = (event) => event.system.map((part) => part.text);
const stepStarted = (sessionID, providerID, id) => (
  {type: "session.step.started", data: {sessionID, model: {providerID, id}}});
const stepEnded = (sessionID, tokens) => ({type: "session.step.ended", data: {sessionID, tokens}});
const compactionEnded = (sessionID) => ({type: "session.compaction.ended", data: {sessionID}});
// session `context` 이벤트를 처리기에 넘기고 실린 시스템 문구(type text 조각)의 글을 output.system 에 모은다.
async function deliverTo(handler, sessionID, output) {
  const event = contextEvent(sessionID);
  await handler.context(event);
  for (const part of event.system) {
    if (part.type !== "text") throw new Error(`시스템 문구 조각 형식 ${part.type}`);
    output.system.push(part.text);
  }
}
'''
