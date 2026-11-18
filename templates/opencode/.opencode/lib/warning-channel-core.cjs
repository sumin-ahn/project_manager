// OpenCode 가드 플러그인이 함께 쓰는 비차단 경고 전달.
// 세션별 경고 한 칸을 다음 모델 호출의 시스템 문구(session `context` 훅의 system[])에 한 번 싣는다.
// 2.x 서버 플러그인에는 사람에게 띄우는 알림 면이 없어 모델 쪽 전달만 한다.

function sessionKey(value) {
  return value ? String(value) : "__global__";
}

function createWarningChannel() {
  const pending = new Map();

  function publish(sessionID, text) {
    pending.set(sessionKey(sessionID), String(text));
  }

  // session `context` 훅 이벤트: { sessionID, system: [{ type: "text", text }, ...] } 를 고쳐 쓴다.
  function inject(event) {
    const key = sessionKey(event.sessionID);
    const text = pending.get(key) || pending.get("__global__");
    if (!text) return;
    event.system.push({ type: "text", text });
    pending.delete(key);
    if (key !== "__global__") pending.delete("__global__");
  }

  return { inject, publish };
}

module.exports = { createWarningChannel };
