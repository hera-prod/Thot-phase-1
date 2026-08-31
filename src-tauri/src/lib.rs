// Thot Phase 1 — Backend Rust Tauri (IPC + Sidecar Python)
// But : lancer le sidecar Python au démarrage et relayer les JSON Lines vers le frontend

use std::sync::Mutex;
use tauri::{Emitter, Manager};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use serde_json::Value;

struct SidecarHandle(Mutex<Option<CommandChild>>);

// Commande : le frontend appelle invoke("chat_send", {request_id, message, mode, image_paths, conv_id})
#[tauri::command]
async fn chat_send(
    app: tauri::AppHandle,
    request_id: String,
    message: String,
    mode: String,
    image_paths: Option<Vec<String>>,
    conv_id: Option<String>,
) -> Result<(), String> {
    let payload = serde_json::json!({
        "id": request_id,
        "method": "chat.send",
        "params": {
            "message": message,
            "mode": mode,
            "image_paths": image_paths,
            "conv_id": conv_id.unwrap_or(request_id.clone())
        }
    });
    let line = payload.to_string() + "\n";
    if let Some(handle) = app.try_state::<SidecarHandle>() {
        let mut guard = handle.0.lock().map_err(|e| e.to_string())?;
        if let Some(child) = guard.as_mut() {
            child.write(line.as_bytes()).map_err(|e| format!("stdin write failed: {}", e))?;
            println!("[rust] chat_send -> sidecar {}", payload);
            return Ok(());
        }
    }
    // Fallback si sidecar non dispo (dev sans python)
    println!("[rust] chat_send (no sidecar) {}", payload);
    Ok(())
}

// Commande : upload d'un fichier (validation côté Rust avant d'écrire sur disque)
#[tauri::command]
async fn save_upload(app: tauri::AppHandle, request_id: String, filename: String, data: Vec<u8>) -> Result<(String, String), String> {
    const MAX_PDF: usize = 20 * 1024 * 1024;
    const MAX_IMAGE: usize = 5 * 1024 * 1024;
    if data.len() > MAX_PDF {
        return Err("Fichier trop volumineux (max 20MB)".to_string());
    }
    let is_png = data.starts_with(b"\x89PNG");
    let is_jpg = data.starts_with(b"\xFF\xD8\xFF");
    let is_pdf = data.starts_with(b"%PDF");
    // Accepte aussi webp (RIFF) minimal
    let is_webp = data.len() > 12 && &data[0..4] == b"RIFF" && &data[8..12] == b"WEBP";
    if !is_png && !is_jpg && !is_pdf && !is_webp {
        return Err("Type de fichier non autorisé (PNG/JPG/WEBP/PDF uniquement)".to_string());
    }
    // Garde-fou taille image spécifique (P1)
    if (is_png || is_jpg || is_webp) && data.len() > MAX_IMAGE {
        return Err("Image trop volumineuse (max 5MB)".to_string());
    }
    // Écriture réelle dans /tmp/thot/{request_id}/{hash}.ext — aligné avec backend/utils/files.py
    let dir = format!("/tmp/thot/{}", request_id);
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let safe_name = filename.replace(['/', '\\', '\0'], "_");
    // Suffixe fiable via extension originale
    let suffix = std::path::Path::new(&safe_name)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("png")
        .to_lowercase();
    // Hash complet pour dedup et filename
    let hash_hex = {
        use sha2::{Sha256, Digest};
        let mut hasher = Sha256::new();
        hasher.update(&data);
        hex::encode(hasher.finalize())
    };
    let path = format!("{}/{}.{}", dir, hash_hex, suffix);
    std::fs::write(&path, &data).map_err(|e| e.to_string())?;
    let hash = format!("sha256:{}", hash_hex);
    let _ = &app;
    Ok((path, hash))
}

#[tauri::command]
fn list_conversations(limit: Option<i64>, offset: Option<i64>) -> Result<Vec<Value>, String> {
    let db_path = "/home/hera/Musique/Thot v0.8/Thot-phase-1/data/thot-phase-1.db";
    if !std::path::Path::new(db_path).exists() {
        return Ok(vec![]);
    }
    // Utilise python3 pour requêter SQLite sans dépendance rusqlite
    let limit_val = limit.unwrap_or(50).clamp(1, 100);
    let offset_val = offset.unwrap_or(0).max(0);
    let py_code = format!(
        r#"import sqlite3, json, pathlib
db="{db_path}"
con=sqlite3.connect(db)
con.row_factory=sqlite3.Row
cur=con.cursor()
try:
    rows=cur.execute("SELECT id, title, mode, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ? OFFSET ?", ({limit_val},{offset_val})).fetchall()
    # enrich with last preview + count if tables exist
    out=[]
    for r in rows:
        d=dict(r)
        try:
            msg=cur.execute("SELECT content FROM messages WHERE conv_id=? ORDER BY created_at DESC LIMIT 1", (d["id"],)).fetchone()
            d["last_message_preview"]=msg[0][:200] if msg and msg[0] else ""
        except: d["last_message_preview"]=""
        try:
            cnt=cur.execute("SELECT COUNT(*) FROM messages WHERE conv_id=?", (d["id"],)).fetchone()
            d["message_count"]=cnt[0] if cnt else 0
        except: d["message_count"]=0
        # normalise dates en ISO
        for k in ("created_at","updated_at"):
            if d.get(k) is not None: d[k]=str(d[k])
        out.append(d)
    print(json.dumps(out))
except Exception as e:
    print(json.dumps([]))
"#,
        db_path = db_path,
        limit_val = limit_val,
        offset_val = offset_val
    );
    let output = std::process::Command::new("python3")
        .args(["-c", &py_code])
        .output()
        .map_err(|e| e.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if stdout.is_empty() {
        return Ok(vec![]);
    }
    let parsed: Vec<Value> = serde_json::from_str(&stdout).unwrap_or_default();
    Ok(parsed)
}

#[tauri::command]
fn get_conversation(conversation_id: String) -> Result<Option<Value>, String> {
    let db_path = "/home/hera/Musique/Thot v0.8/Thot-phase-1/data/thot-phase-1.db";
    if !std::path::Path::new(db_path).exists() {
        return Ok(None);
    }
    let py_code = format!(
        r#"import sqlite3, json
db="{db}"
con=sqlite3.connect(db)
con.row_factory=sqlite3.Row
cur=con.cursor()
try:
    row=cur.execute("SELECT id, title, mode, created_at, updated_at FROM conversations WHERE id=?", ("{id}",)).fetchone()
    if row:
        d=dict(row)
        for k in ("created_at","updated_at"):
            if d.get(k) is not None: d[k]=str(d[k])
        print(json.dumps(d))
    else:
        print("null")
except Exception as e:
    print("null")
"#,
        db = db_path,
        id = conversation_id.replace('"', "")
    );
    let output = std::process::Command::new("python3")
        .args(["-c", &py_code])
        .output()
        .map_err(|e| e.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if stdout.is_empty() || stdout == "null" {
        return Ok(None);
    }
    let parsed: Value = serde_json::from_str(&stdout).map_err(|e| e.to_string())?;
    Ok(Some(parsed))
}

#[tauri::command]
fn list_messages(conversation_id: String) -> Result<Vec<Value>, String> {
    let db_path = "/home/hera/Musique/Thot v0.8/Thot-phase-1/data/thot-phase-1.db";
    if !std::path::Path::new(db_path).exists() {
        return Ok(vec![]);
    }
    let py_code = format!(
        r#"import sqlite3, json
db="{db}"
con=sqlite3.connect(db)
con.row_factory=sqlite3.Row
cur=con.cursor()
try:
    rows=cur.execute("SELECT id, conv_id, role, content, thinking, created_at FROM messages WHERE conv_id=? ORDER BY created_at ASC", ("{id}",)).fetchall()
    out=[dict(r) for r in rows]
    for d in out:
        for k in ("created_at",):
            if d.get(k) is not None: d[k]=str(d[k])
    print(json.dumps(out))
except Exception as e:
    print(json.dumps([]))
"#,
        db = db_path,
        id = conversation_id.replace('"', "")
    );
    let output = std::process::Command::new("python3")
        .args(["-c", &py_code])
        .output()
        .map_err(|e| e.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if stdout.is_empty() {
        return Ok(vec![]);
    }
    let parsed: Vec<Value> = serde_json::from_str(&stdout).unwrap_or_default();
    Ok(parsed)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let (mut rx, child) = if cfg!(debug_assertions) {
                app.shell()
                    .command("python3")
                    .args(["-m", "backend.main"])
                    .current_dir("/home/hera/Musique/Thot v0.8/Thot-phase-1")
                    .spawn()
                    .expect("échec lancement python3 -m backend.main (vérifie pip install -r requirements.txt)")
            } else {
                let sidecar = app.shell()
                    .sidecar("thot-sidecar")
                    .expect("sidecar thot-sidecar introuvable dans src-tauri/binaries/");
                sidecar.spawn().expect("échec lancement sidecar Python")
            };

            app.manage(SidecarHandle(Mutex::new(Some(child))));

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        CommandEvent::Stdout(line) => {
                            let text = String::from_utf8_lossy(&line).to_string();
                            for l in text.lines() {
                                if let Ok(v) = serde_json::from_str::<Value>(l) {
                                    if let Some(ev) = v.get("event").and_then(|e| e.as_str()) {
                                        let payload = v.get("payload").cloned().unwrap_or(Value::Null);
                                        let _ = handle.emit(ev, payload);
                                    }
                                }
                            }
                        }
                        CommandEvent::Stderr(line) => {
                            eprintln!("[python] {}", String::from_utf8_lossy(&line));
                        }
                        CommandEvent::Terminated(s) => {
                            eprintln!("[sidecar] terminé : {:?}", s);
                            break;
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![chat_send, save_upload, list_conversations, get_conversation, list_messages])
        .run(tauri::generate_context!())
        .expect("erreur lors du lancement de Tauri");
}
