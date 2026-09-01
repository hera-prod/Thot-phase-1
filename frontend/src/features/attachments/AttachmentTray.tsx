import type { PendingAttachment } from "./attachmentTypes";

export function AttachmentTray({ attachments, onRemove }: { attachments: PendingAttachment[]; onRemove: (id: string) => void }) {
  if (attachments.length === 0) return null;
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
      {attachments.map((a) => (
        <div key={a.id} style={{ position: "relative", width: 72, height: 72, borderRadius: 8, overflow: "hidden", border: "1px solid var(--thot-border)", background: "var(--thot-surface-2)", display: "flex", alignItems: "center", justifyContent: "center" }}>
          {a.kind === "pdf" ? <span style={{ fontSize: 28 }}>📄</span> : <img src={a.previewUrl} alt={a.name} style={{ width: "100%", height: "100%", objectFit: "cover" }} />}
          <button onClick={() => onRemove(a.id)} aria-label="Retirer" style={{ position: "absolute", top: 2, right: 2, width: 20, height: 20, borderRadius: "50%", border: "none", background: "rgba(0,0,0,0.6)", color: "#fff", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12 }}>×</button>
          <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, background: "rgba(0,0,0,0.55)", color: "#fff", fontSize: 9, padding: "2px 4px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{a.name}</div>
        </div>
      ))}
    </div>
  );
}
