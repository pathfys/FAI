import { useState, useRef, useEffect, useCallback } from "react";

const SYSTEM_PROMPT = `Ты — владелец этого Telegram аккаунта. Отвечай кратко, по делу, дружелюбно, на языке собеседника.
Если не знаешь ответ — скажи что владелец ответит позже.
Не говори что ты бот или ИИ. Не используй эмодзи чрезмерно.
Отвечай максимально коротко — 1-3 предложения. Как реальный человек в мессенджере.`;

const PRAISE = new Set(["хорошо","отлично","класс","збс","норм","огонь","топ","круто","заебись","четко","чётко","молодец","пушка","+","👍","🔥","да","ок","лайк","good","nice","great","perfect","awesome","супер"]);
const BAD = new Set(["плохо","хуйня","говно","не то","мимо","фу","переделай","криво","тупо","-","👎","bad","wrong","nope","не так","бред","не катит"]);

function classify(text) {
  const t = text.trim().toLowerCase();
  if (PRAISE.has(t) || [...PRAISE].some(w => t.includes(w))) return "good";
  if (BAD.has(t) || [...BAD].some(w => t.includes(w))) return "bad";
  return null;
}

async function callAI(messages) {
  const body = {
    model: "claude-sonnet-4-6",
    max_tokens: 400,
    system: SYSTEM_PROMPT,
    messages,
  };
  const res = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  return (data.content || []).map(b => b.text || "").join("").trim();
}

const TS = () => {
  const d = new Date();
  return `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}`;
};

export default function AgentTester() {
  const [lines, setLines] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [history, setHistory] = useState([]);
  const [stats, setStats] = useState({ total: 0, good: 0, bad: 0, latency: [] });
  const [awaitFB, setAwaitFB] = useState(false);
  const [lastAnswer, setLastAnswer] = useState("");
  const endRef = useRef(null);
  const inputRef = useRef(null);

  const scroll = () => setTimeout(() => endRef.current?.scrollIntoView({ behavior: "smooth" }), 50);
  
  const addLine = useCallback((type, text, meta) => {
    setLines(prev => [...prev, { type, text, meta, ts: TS() }]);
    scroll();
  }, []);

  useEffect(() => {
    addLine("system", "═══════════════════════════════════════════════════════");
    addLine("header", "🤖 BUSINESS AGENT — ТЕСТЕР АВТООТВЕТЧИКА");
    addLine("system", "═══════════════════════════════════════════════════════");
    addLine("info", "Пиши как собеседник в Telegram. Бот ответит в стиле владельца.");
    addLine("info", "После ответа оцени: 👍 хорошо / 👎 плохо / или новый вопрос.");
    addLine("dim", "───────────────────────────────────────────────────────");
    addLine("cmd", "/stats — статистика  /reset — сбросить  /prompt — промпт");
    addLine("dim", "───────────────────────────────────────────────────────");
    addLine("success", "✅ Модель: claude-sonnet-4-6 (бесплатно в артефакте)");
    addLine("dim", "");
  }, []);

  useEffect(() => {
    if (!loading) inputRef.current?.focus();
  }, [loading]);

  const handleSubmit = async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput("");

    // Commands
    if (text === "/stats") {
      const { total, good, bad, latency } = stats;
      const avg = latency.length ? (latency.reduce((a,b) => a+b, 0) / latency.length).toFixed(1) : "—";
      const pct = total ? ((good / total) * 100).toFixed(0) : 0;
      const barLen = 25;
      const filled = total ? Math.round(barLen * pct / 100) : 0;
      addLine("dim", "───────────────────────────────────────────────────────");
      addLine("header", "📊 СТАТИСТИКА");
      addLine("info", `├─ Ответов: ${total}`);
      addLine("success", `├─ 👍 Хорошие: ${good} (${pct}%)`);
      addLine("error", `├─ 👎 Плохие: ${bad}`);
      addLine("info", `├─ ⏱ Среднее время: ${avg} сек`);
      addLine("info", `└─ 🤖 Модель: claude-sonnet-4-6`);
      const bar = "█".repeat(filled) + "░".repeat(barLen - filled);
      addLine(pct >= 70 ? "success" : pct >= 40 ? "warn" : "error", `  [${bar}] ${pct}% одобрения`);
      addLine("dim", "───────────────────────────────────────────────────────");
      return;
    }
    if (text === "/reset") {
      setHistory([]);
      setAwaitFB(false);
      addLine("warn", "🧹 История очищена.");
      return;
    }
    if (text === "/prompt") {
      addLine("dim", "───────────────────────────────────────────────────────");
      addLine("header", "📝 SYSTEM PROMPT:");
      SYSTEM_PROMPT.split("\n").forEach(l => addLine("dim", "  " + l));
      addLine("dim", "───────────────────────────────────────────────────────");
      return;
    }

    // Feedback?
    if (awaitFB) {
      const fb = classify(text);
      if (fb === "good") {
        setStats(s => ({ ...s, good: s.good + 1 }));
        addLine("success", "  ✅ Записал: хороший ответ");
        addLine("dim", "");
        setAwaitFB(false);
        return;
      }
      if (fb === "bad") {
        setStats(s => ({ ...s, bad: s.bad + 1 }));
        addLine("error", "  ❌ Записал: плохой ответ");
        addLine("dim", "");
        setAwaitFB(false);
        return;
      }
      setAwaitFB(false);
    }

    // User message
    addLine("user", text);
    const newHistory = [...history, { role: "user", content: text }];
    setHistory(newHistory);
    setLoading(true);

    const t0 = Date.now();
    try {
      const answer = await callAI(newHistory);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      const num = stats.total + 1;

      if (!answer) {
        addLine("error", "  ❌ Модель не ответила");
        setLoading(false);
        return;
      }

      setStats(s => ({
        ...s,
        total: s.total + 1,
        latency: [...s.latency, parseFloat(elapsed)],
      }));
      setHistory([...newHistory, { role: "assistant", content: answer }]);
      setLastAnswer(answer);

      addLine("bot", answer, { elapsed, num });
      addLine("dim", "");
      setAwaitFB(true);
    } catch (err) {
      addLine("error", `  ❌ Ошибка: ${err.message}`);
    }
    setLoading(false);
  };

  const renderLine = (line, i) => {
    const styles = {
      system:  { color: "#4a5568" },
      header:  { color: "#38bdf8", fontWeight: 700, fontSize: 15 },
      info:    { color: "#94a3b8" },
      dim:     { color: "#334155" },
      cmd:     { color: "#64748b", fontStyle: "italic" },
      success: { color: "#4ade80" },
      warn:    { color: "#facc15" },
      error:   { color: "#f87171" },
      user:    { color: "#38bdf8" },
      bot:     { color: "#4ade80" },
      fb:      { color: "#a78bfa" },
    };

    if (line.type === "user") {
      return (
        <div key={i} style={{ display: "flex", gap: 8, padding: "4px 0" }}>
          <span style={{ color: "#475569", fontSize: 11, minWidth: 55, fontFamily: "monospace" }}>{line.ts}</span>
          <span style={{ color: "#38bdf8", fontWeight: 600 }}>👤 Ты ▸</span>
          <span style={{ color: "#e2e8f0" }}>{line.text}</span>
        </div>
      );
    }

    if (line.type === "bot") {
      return (
        <div key={i} style={{ padding: "6px 0" }}>
          <div style={{ display: "flex", gap: 8 }}>
            <span style={{ color: "#475569", fontSize: 11, minWidth: 55, fontFamily: "monospace" }}>{line.ts}</span>
            <span style={{ color: "#4ade80", fontWeight: 600 }}>🤖 Бот ▸</span>
            <span style={{ color: "#bbf7d0", lineHeight: 1.5 }}>{line.text}</span>
          </div>
          <div style={{ marginLeft: 63, color: "#475569", fontSize: 11, marginTop: 2 }}>
            ⏱ {line.meta?.elapsed}s │ #{line.meta?.num} │ оцени ответ 👍👎
          </div>
        </div>
      );
    }

    return (
      <div key={i} style={{ ...styles[line.type], padding: "1px 0", paddingLeft: line.type === "dim" ? 0 : 0 }}>
        {line.text}
      </div>
    );
  };

  const goodPct = stats.total ? Math.round((stats.good / stats.total) * 100) : 0;

  return (
    <div style={{
      background: "#0a0f1a",
      minHeight: "100vh",
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
      fontSize: 13,
      color: "#cbd5e1",
      display: "flex",
      flexDirection: "column",
    }}>
      {/* Top bar */}
      <div style={{
        background: "#111827",
        borderBottom: "1px solid #1e293b",
        padding: "8px 16px",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        flexShrink: 0,
      }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <div style={{ width: 10, height: 10, borderRadius: "50%", background: "#ef4444" }} />
          <div style={{ width: 10, height: 10, borderRadius: "50%", background: "#eab308" }} />
          <div style={{ width: 10, height: 10, borderRadius: "50%", background: "#22c55e" }} />
          <span style={{ marginLeft: 12, color: "#64748b", fontSize: 12 }}>business_agent_tester — bash</span>
        </div>
        <div style={{ display: "flex", gap: 16, fontSize: 11, color: "#64748b" }}>
          <span>📊 {stats.total} ответов</span>
          <span style={{ color: "#4ade80" }}>👍 {stats.good}</span>
          <span style={{ color: "#f87171" }}>👎 {stats.bad}</span>
          {stats.total > 0 && (
            <span style={{ color: goodPct >= 70 ? "#4ade80" : goodPct >= 40 ? "#facc15" : "#f87171" }}>
              {goodPct}%
            </span>
          )}
        </div>
      </div>

      {/* Terminal body */}
      <div style={{
        flex: 1,
        overflowY: "auto",
        padding: "12px 16px",
      }}
        onClick={() => inputRef.current?.focus()}
      >
        {lines.map(renderLine)}
        
        {loading && (
          <div style={{ color: "#64748b", padding: "4px 0", display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontSize: 11, minWidth: 55 }}>{TS()}</span>
            <span className="blink" style={{ color: "#facc15" }}>⏳ Думаю...</span>
          </div>
        )}
        
        <div ref={endRef} />
      </div>

      {/* Input */}
      <div style={{
        borderTop: "1px solid #1e293b",
        padding: "10px 16px",
        display: "flex",
        gap: 8,
        alignItems: "center",
        background: "#111827",
        flexShrink: 0,
      }}>
        <span style={{ color: awaitFB ? "#a78bfa" : "#38bdf8", fontWeight: 600, whiteSpace: "nowrap" }}>
          {awaitFB ? "оценка ▸" : "👤 ▸"}
        </span>
        <input
          ref={inputRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !loading && input.trim()) handleSubmit(); }}
          disabled={loading}
          placeholder={awaitFB ? "👍 / 👎 / или новый вопрос..." : "напиши сообщение..."}
          style={{
            flex: 1,
            background: "transparent",
            border: "none",
            outline: "none",
            color: "#e2e8f0",
            fontSize: 13,
            fontFamily: "inherit",
            caretColor: "#38bdf8",
          }}
        />
        <button
          type="button"
          onClick={handleSubmit}
          disabled={loading || !input.trim()}
          style={{
            background: loading ? "#1e293b" : "#1d4ed8",
            color: "#fff",
            border: "none",
            borderRadius: 4,
            padding: "4px 12px",
            fontSize: 12,
            fontFamily: "inherit",
            cursor: loading ? "not-allowed" : "pointer",
            opacity: loading || !input.trim() ? 0.4 : 1,
          }}
        >
          ⏎
        </button>
      </div>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #0a0f1a; }
        ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 3px; }
        .blink { animation: blink 1s infinite; }
        @keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
      `}</style>
    </div>
  );
}
