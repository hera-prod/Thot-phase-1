// Thot Phase 1 — Backend Rust Tauri (IPC + Sidecar Python)
// Contrat portable (C19 / portabilite.md) : portable_strict résout depuis lanceur, jamais CWD.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tauri::{Emitter, Manager};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use serde_json::Value;

struct SidecarHandle(Mutex<Option<CommandChild>>);

// ---------- Portabilité : détection racine ----------

fn find_portable_root(start: &Path) -> Option<PathBuf> {
    // Cherche portable.json en remontant 2 niveaux depuis start
    let mut cur = Some(start.to_path_buf());
    for _ in 0..3 {
        if let Some(p) = cur {
            if p.join("portable.json").exists() {
                return Some(p);
            }
            if p.join("data").join("portable.json").exists() {
                return Some(p);
            }
            cur = p.parent().map(|x| x.to_path_buf());
        } else {
            break;
        }
    }
    None
}

fn resolve_app_data_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    // 1. Portable : cherche autour de l'exe et du resource_dir
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            if let Some(root) = find_portable_root(parent) {
                println!("[rust] portable root (exe) : {}", root.display());
                return Ok(root);
            }
        }
    }
    // resource_dir (Tauri) peut être différent en dev/prod
    if let Ok(res) = app.path().resource_dir() {
        if let Some(root) = find_portable_root(&res) {
            println!("[rust] portable root (resource) : {}", root.display());
            return Ok(root);
        }
        // En dev, resource_dir = .../src-tauri, on remonte
        if let Some(parent) = res.parent() {
            if let Some(root) = find_portable_root(parent) {
                return Ok(root);
            }
        }
    }
    // 2. Mode installé : XDG / AppData
    let dir = app.path().app_data_dir().map_err(|e| format!("Impossible de résoudre app_data_dir: {}", e))?;
    Ok(dir)
}

fn resolve_data_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let base = resolve_app_data_dir(app)?;
    // Si base est racine portable (contient portable.json), data = base/data
    // Sinon base est déjà app_data_dir -> data = base/data
    let data_dir = if base.join("portable.json").exists() {
        base.join("data")
    } else if base.file_name().map(|n| n == "data").unwrap_or(false) {
        base
    } else {
        base.join("data")
    };
    fs::create_dir_all(&data_dir).map_err(|e| format!("Impossible de créer data dir {}: {}", data_dir.display(), e))?;
    // Sous-dossiers contrat portable (database/blobs/logs/temp)
    for sub in ["database", "blobs", "logs", "temp", "backups"] {
        let _ = fs::create_dir_all(data_dir.join(sub));
    }
    // Compat : ancien dossier media -> blobs (migration douce, on garde media si existe)
    Ok(data_dir)
}

fn resolve_db_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let data_dir = resolve_data_dir(app)?;
    // Nouveau chemin canonique : data/database/thot-phase-1.db
    let new_db = data_dir.join("database").join("thot-phase-1.db");
    // Compat : data/thot-phase-1.db (ancien)
    let old_db = data_dir.join("thot-phase-1.db");
    if new_db.exists() {
        println!("[rust] Thot DB path (new): {}", new_db.display());
        return Ok(new_db);
    }
    if old_db.exists() {
        println!("[rust] Thot DB path (legacy): {}", old_db.display());
        return Ok(old_db);
    }
    // Par défaut : nouveau chemin
    println!("[rust] Thot DB path (default new): {}", new_db.display());
    Ok(new_db)
}

fn resolve_temp_dir(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(data_dir) = resolve_data_dir(app) {
        // Vérifie si portable (portable.json à côté)
        if let Ok(base) = resolve_app_data_dir(app) {
            if base.join("portable.json").exists() {
                return data_dir.join("temp");
            }
        }
    }
    std::env::temp_dir().join("thot")
}

// ---------- IPC : chat_send ----------

#[tauri::command]
async fn chat_send(
    app: tauri::AppHandle,
    request_id: String,
    message: String,
    mode: String,
    image_paths: Option<Vec<String>>,
    attachment_ids: Option<Vec<String>>,
    conv_id: Option<String>,
) -> Result<(), String> {
    // Pont Frontend → Python : le front parle camelCase (requestId...), Rust sérialise en
    // snake_case vers le sidecar via stdin JSON Lines. "id" (et pas "request_id") = format
    // legacy accepté par backend/inference/ipc.py qui normalise id → request_id (C08).
    // conv_id.unwrap_or(request_id) : une nouvelle conversation naît avec l'id du run —
    // le front, lui, fige ensuite ce conv_id (currentConvIdRef) pour les tours suivants.
    let payload = serde_json::json!({
        "id": request_id,
        "method": "chat.send",
        "params": {
            "message": message,
            "mode": mode,
            "image_paths": image_paths,
            "attachment_ids": attachment_ids,
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
    Err("Sidecar Python non disponible — redémarrez l'application".to_string())
}

// ---------- Upload ----------

#[tauri::command]
async fn save_upload(app: tauri::AppHandle, request_id: String, filename: String, data: Vec<u8>) -> Result<(String, String), String> {
    // Limites basées modèle vision (Depot + NaFlex 512) : pas de 5 Mo dur, resize tracé côté Python
    // On accepte jusqu'à 20 Mo (même que PDF), le backend prétraite (thumbnail 1536, JPEG88)
    const MAX_PDF: usize = 20 * 1024 * 1024;
    const MAX_IMAGE: usize = 20 * 1024 * 1024;
    if data.len() > MAX_PDF {
        return Err("Fichier trop volumineux (max 20MB)".to_string());
    }
    let is_png = data.starts_with(b"\x89PNG");
    let is_jpg = data.starts_with(b"\xFF\xD8\xFF");
    let is_pdf = data.starts_with(b"%PDF");
    let is_webp = data.len() > 12 && &data[0..4] == b"RIFF" && &data[8..12] == b"WEBP";
    if !is_png && !is_jpg && !is_pdf && !is_webp {
        return Err("Type de fichier non autorisé (PNG/JPG/WEBP/PDF uniquement)".to_string());
    }
    if (is_png || is_jpg || is_webp) && data.len() > MAX_IMAGE {
        return Err("Image trop volumineuse (max 5MB)".to_string());
    }
    // Dossier temp résolu selon mode portable
    let base_tmp = resolve_temp_dir(&app);
    let dir = base_tmp.join(&request_id);
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let safe_name = filename.replace(['/', '\\', '\0'], "_");
    let suffix = std::path::Path::new(&safe_name)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("png")
        .to_lowercase();
    let hash_hex = {
        use sha2::{Sha256, Digest};
        let mut hasher = Sha256::new();
        hasher.update(&data);
        hex::encode(hasher.finalize())
    };
    let path = dir.join(format!("{}.{}", hash_hex, suffix));
    std::fs::write(&path, &data).map_err(|e| e.to_string())?;
    let hash = format!("sha256:{}", hash_hex);
    Ok((path.to_string_lossy().to_string(), hash))
}

// ---------- DB helpers (rusqlite, plus de python3 -c) ----------

fn open_db(app: &tauri::AppHandle) -> Result<rusqlite::Connection, String> {
    let db_path = resolve_db_path(app)?;
    if !db_path.exists() {
        return Err(format!("DB absente: {}", db_path.display()));
    }
    rusqlite::Connection::open(&db_path).map_err(|e| e.to_string())
}

#[tauri::command]
fn list_conversations(app: tauri::AppHandle, limit: Option<i64>, offset: Option<i64>) -> Result<Vec<Value>, String> {
    let conn = match open_db(&app) {
        Ok(c) => c,
        Err(e) => {
            // DB pas encore créée → liste vide (premier lancement)
            println!("[rust] list_conversations DB not ready: {}", e);
            return Ok(vec![]);
        }
    };
    let limit_val = limit.unwrap_or(50).clamp(1, 100);
    let offset_val = offset.unwrap_or(0).max(0);
    let mut stmt = conn.prepare("SELECT id, title, mode, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?1 OFFSET ?2").map_err(|e| e.to_string())?;
    let rows = stmt.query_map(rusqlite::params![limit_val, offset_val], |row| {
        Ok(serde_json::json!({
            "id": row.get::<_, String>(0)?,
            "title": row.get::<_, String>(1)?,
            "mode": row.get::<_, Option<String>>(2)?,
            "created_at": row.get::<_, String>(3)?,
            "updated_at": row.get::<_, String>(4)?,
        }))
    }).map_err(|e| e.to_string())?;
    let mut out: Vec<Value> = Vec::new();
    for r in rows {
        let mut v = r.map_err(|e| e.to_string())?;
        // enrichir avec last_message_preview + message_count (best effort)
        if let Some(id) = v.get("id").and_then(|x| x.as_str()).map(|s| s.to_string()) {
            if let Ok(mut s2) = conn.prepare("SELECT content FROM messages WHERE conv_id=?1 ORDER BY created_at DESC LIMIT 1") {
                if let Ok(mut rows2) = s2.query(rusqlite::params![id]) {
                    if let Ok(Some(row2)) = rows2.next() {
                        let preview: String = row2.get(0).unwrap_or_default();
                        v["last_message_preview"] = Value::String(preview.chars().take(200).collect());
                    } else {
                        v["last_message_preview"] = Value::String(String::new());
                    }
                }
            }
            if let Ok(mut s3) = conn.prepare("SELECT COUNT(*) FROM messages WHERE conv_id=?1") {
                if let Ok(mut rows3) = s3.query(rusqlite::params![id]) {
                    if let Ok(Some(row3)) = rows3.next() {
                        let cnt: i64 = row3.get(0).unwrap_or(0);
                        v["message_count"] = Value::Number(cnt.into());
                    }
                }
            }
        }
        if v.get("last_message_preview").is_none() {
            v["last_message_preview"] = Value::String(String::new());
        }
        if v.get("message_count").is_none() {
            v["message_count"] = Value::Number(0.into());
        }
        out.push(v);
    }
    Ok(out)
}

#[tauri::command]
fn get_conversation(app: tauri::AppHandle, conversation_id: String) -> Result<Option<Value>, String> {
    let conn = match open_db(&app) {
        Ok(c) => c,
        Err(_) => return Ok(None),
    };
    let mut stmt = conn.prepare("SELECT id, title, mode, created_at, updated_at FROM conversations WHERE id=?1").map_err(|e| e.to_string())?;
    let mut rows = stmt.query(rusqlite::params![conversation_id]).map_err(|e| e.to_string())?;
    if let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let v = serde_json::json!({
            "id": row.get::<_, String>(0).unwrap_or_default(),
            "title": row.get::<_, String>(1).unwrap_or_default(),
            "mode": row.get::<_, Option<String>>(2).unwrap_or(None),
            "created_at": row.get::<_, String>(3).unwrap_or_default(),
            "updated_at": row.get::<_, String>(4).unwrap_or_default(),
        });
        Ok(Some(v))
    } else {
        Ok(None)
    }
}

#[tauri::command]
fn list_messages(app: tauri::AppHandle, conversation_id: String) -> Result<Vec<Value>, String> {
    let conn = match open_db(&app) {
        Ok(c) => c,
        Err(_) => return Ok(vec![]),
    };
    let mut stmt = conn.prepare("SELECT id, conv_id, role, content, thinking, created_at FROM messages WHERE conv_id=?1 ORDER BY created_at ASC").map_err(|e| e.to_string())?;
    let rows = stmt.query_map(rusqlite::params![conversation_id], |row| {
        Ok(serde_json::json!({
            "id": row.get::<_, String>(0)?,
            "conv_id": row.get::<_, String>(1)?,
            "role": row.get::<_, String>(2)?,
            "content": row.get::<_, String>(3)?,
            "thinking": row.get::<_, Option<String>>(4)?,
            "created_at": row.get::<_, String>(5)?,
        }))
    }).map_err(|e| e.to_string())?;
    // Pièces jointes par message : artefacts durables (annexe 10) + sources legacy.
    // D'OÙ VIENNENT LES LIGNES : message_attachments (écrit par le sidecar Python dans main.py)
    // + sources (ancien pipeline image_paths, compat). OÙ VONT-ELLES : champ "attachments" de
    // chaque message → le front recharge les vignettes via get_attachment_bytes.
    // POURQUOI PAS DE CHEMINS DANS LA RÉPONSE : on ne renvoie que des ids + noms ; les bytes
    // transitent par get_attachment_bytes (pas de fuite de chemins disque vers l'UI, pas de
    // JSON de plusieurs Mo dans list_messages, et pas de dépendance au scope asset://).
    let mut att_by_msg: std::collections::HashMap<String, Vec<Value>> = std::collections::HashMap::new();
    if let Ok(mut s_att) = conn.prepare(
        "SELECT ma.message_id, a.id, a.kind, COALESCE(a.original_filename, a.display_name, a.id), COALESCE(b.mime_detected, '') \
         FROM message_attachments ma \
         JOIN messages m ON m.id = ma.message_id \
         JOIN artifacts a ON a.id = ma.artifact_id \
         JOIN blobs b ON b.id = a.blob_id \
         WHERE m.conv_id = ?1 ORDER BY ma.ordinal ASC"
    ) {
        if let Ok(map) = s_att.query_map(rusqlite::params![conversation_id], |row| {
            Ok((row.get::<_, String>(0)?, serde_json::json!({
                "id": row.get::<_, String>(1)?,
                "kind": row.get::<_, String>(2)?,
                "name": row.get::<_, String>(3)?,
                "mime": row.get::<_, String>(4)?,
            })))
        }) {
            for r in map.flatten() {
                att_by_msg.entry(r.0).or_default().push(r.1);
            }
        }
    }
    if let Ok(mut s_src) = conn.prepare(
        "SELECT msg_id, id, media_kind, COALESCE(original_name, id), COALESCE(mime_type, '') \
         FROM sources WHERE conversation_id = ?1"
    ) {
        if let Ok(map) = s_src.query_map(rusqlite::params![conversation_id], |row| {
            Ok((row.get::<_, String>(0)?, serde_json::json!({
                "id": row.get::<_, String>(1)?,
                "kind": row.get::<_, String>(2)?,
                "name": row.get::<_, String>(3)?,
                "mime": row.get::<_, String>(4)?,
            })))
        }) {
            for r in map.flatten() {
                let entry = att_by_msg.entry(r.0).or_default();
                if !entry.iter().any(|v| v.get("id") == r.1.get("id")) {
                    entry.push(r.1);
                }
            }
        }
    }
    let mut out = Vec::new();
    for r in rows {
        let mut v = r.map_err(|e| e.to_string())?;
        let mid = v.get("id").and_then(|x| x.as_str()).unwrap_or("").to_string();
        v["attachments"] = att_by_msg.remove(&mid).map(Value::Array).unwrap_or(Value::Array(vec![]));
        out.push(v);
    }
    Ok(out)
}

#[tauri::command]
async fn get_attachment_bytes(app: tauri::AppHandle, artifact_id: String) -> Result<(Vec<u8>, String), String> {
    // Vignettes d'historique : bytes du blob (annexe 10) ou source legacy.
    // D'OÙ VIENT L'ID : champ "attachments[].id" de list_messages. OÙ VONT LES BYTES : le
    // front fabrique un objectURL pour <img> (même motif que get_staged_preview_bytes).
    // POURQUOI DES BYTES ET PAS convertFileSrc : aucun pari sur le scope du protocole
    // asset:// (data/ vs tmp/) ; lecture disque incontestable côté Rust, cap 10 Mo identique.
    // Cap 10 Mo comme get_staged_preview_bytes.
    let data_dir = resolve_data_dir(&app)?;
    let conn = open_db(&app).map_err(|e| e.to_string())?;
    let mut rel: Option<String> = None;
    let mut mime = String::from("application/octet-stream");
    if let Ok(mut s) = conn.prepare(
        "SELECT b.relative_path, COALESCE(b.mime_detected, '') FROM artifacts a JOIN blobs b ON b.id = a.blob_id WHERE a.id = ?1"
    ) {
        if let Ok(mut rows) = s.query(rusqlite::params![artifact_id]) {
            if let Ok(Some(row)) = rows.next() {
                rel = row.get(0).ok();
                mime = row.get(1).unwrap_or(mime);
            }
        }
    }
    if rel.is_none() {
        if let Ok(mut s2) = conn.prepare("SELECT relative_path, COALESCE(mime_type, '') FROM sources WHERE id = ?1") {
            if let Ok(mut rows2) = s2.query(rusqlite::params![artifact_id]) {
                if let Ok(Some(row2)) = rows2.next() {
                    rel = row2.get(0).ok();
                    mime = row2.get(1).unwrap_or(mime);
                }
            }
        }
    }
    let rel = rel.ok_or_else(|| "Pièce jointe introuvable".to_string())?;
    let p = data_dir.join(&rel);
    if !p.exists() || !p.is_file() {
        return Err("Fichier joint introuvable sur disque".to_string());
    }
    let bytes = fs::read(&p).map_err(|e| e.to_string())?;
    if bytes.len() > 10 * 1024 * 1024 {
        return Err("Fichier trop volumineux pour la vignette (10 Mo)".to_string());
    }
    Ok((bytes, mime))
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct StagedFile {
    staged_path: String,
    name: String,
    mime: String,
    size: u64,
    kind: String, // image | pdf
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct Preview {
    kind: String,
    token: String,
    width: Option<u32>,
    height: Option<u32>,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct StagingItem {
    stage_item_id: String,
    filename: String,
    display_name: String,
    mime_detected: String,
    size_bytes: u64,
    sha256: String,
    preview: Option<Preview>,
    status: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct StagingResponse {
    stage_id: String,
    items: Vec<StagingItem>,
    rejected: Vec<serde_json::Value>,
}

#[tauri::command]
async fn stage_dropped_files(app: tauri::AppHandle, paths: Vec<String>, draft_id: Option<String>) -> Result<Vec<StagedFile>, String> {
    // Validation C12/C18 + annexe 09 : NaFlex 512, pas de 5 Mo dur → resize tracé Python
    const MAX_IMAGE: u64 = 20 * 1024 * 1024;
    const MAX_PDF: u64 = 20 * 1024 * 1024;
    const MAX_FILES: usize = 4;
    let _draft = draft_id.unwrap_or_else(|| "global".to_string());
    if paths.len() > MAX_FILES {
        return Err(format!("Trop de fichiers (max {}), reçu {}", MAX_FILES, paths.len()));
    }
    // Dossier staging : data/temp/staged
    let temp_base = resolve_temp_dir(&app);
    let staged_dir = temp_base.join("staged");
    fs::create_dir_all(&staged_dir).map_err(|e| e.to_string())?;
    let mut out: Vec<StagedFile> = Vec::new();
    for p in paths {
        let src = PathBuf::from(&p);
        // Dossiers et raccourcis refusés (annexe 09 §6)
        if src.is_dir() {
            return Err(format!("Dossier non supporté : {}", src.display()));
        }
        let ext = src.extension().and_then(|s| s.to_str()).unwrap_or("").to_lowercase();
        if ext == "lnk" {
            return Err(format!("Raccourci .lnk non supporté : {}", src.display()));
        }
        let meta = fs::metadata(&src).map_err(|e| format!("Fichier inaccessible {} : {}", src.display(), e))?;
        if meta.len() == 0 {
            return Err(format!("Fichier vide : {}", src.display()));
        }
        if meta.len() > MAX_PDF {
            return Err(format!("Fichier trop volumineux (>20 Mo) : {}", src.display()));
        }
        // Lecture magic bytes pour MIME réel (C12)
        let mut header = [0u8; 12];
        let is_png = fs::File::open(&src).and_then(|mut f| { use std::io::Read; f.read_exact(&mut header).map(|_| header) }).map(|h| h[0..8] == [0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A]).unwrap_or(false);
        let is_jpg = header[0..3] == [0xFF,0xD8,0xFF];
        let is_pdf = header[0..4] == [0x25,0x50,0x44,0x46]; // %PDF
        let is_webp = header[0..4] == [0x52,0x49,0x46,0x46] && header[8..12] == [0x57,0x45,0x42,0x50];
        let (mime, kind) = if is_png { ("image/png","image") } else if is_jpg { ("image/jpeg","image") } else if is_webp { ("image/webp","image") } else if is_pdf { ("application/pdf","pdf") } else {
            return Err(format!("Type non pris en charge (PNG/JPG/WEBP/PDF) : {}", src.display()));
        };
        if kind == "image" && meta.len() > MAX_IMAGE {
            return Err(format!("Image trop volumineuse (>5 Mo) : {}", src.display()));
        }
        // Hash + copie vers staged
        let hash_hex = {
            use sha2::{Sha256, Digest};
            let mut hasher = Sha256::new();
            let bytes = fs::read(&src).map_err(|e| e.to_string())?;
            hasher.update(&bytes);
            hex::encode(hasher.finalize())
        };
        let staged_path = staged_dir.join(format!("{}.{}", hash_hex, ext));
        if !staged_path.exists() {
            fs::copy(&src, &staged_path).map_err(|e| e.to_string())?;
        }
        let name = src.file_name().and_then(|s| s.to_str()).unwrap_or("fichier").to_string();
        out.push(StagedFile { staged_path: staged_path.to_string_lossy().to_string(), name, mime: mime.to_string(), size: meta.len(), kind: kind.to_string() });
    }
    let _ = &app;
    Ok(out)
}

// Annexe 10 : pipeline unifié — même forme pour drag, trombone, collage, capture
#[tauri::command]
async fn stage_dropped_paths(app: tauri::AppHandle, request_id: String, paths: Vec<String>) -> Result<StagingResponse, String> {
    // Réutilise la logique de stage_dropped_files mais retourne la forme cible annexe 10
    let temp_base = resolve_temp_dir(&app);
    let staged_dir = temp_base.join("staged");
    fs::create_dir_all(&staged_dir).map_err(|e| e.to_string())?;
    let stage_id = format!("stg_{}", &request_id[..8.min(request_id.len())]);
    let mut items: Vec<StagingItem> = Vec::new();
    let mut rejected: Vec<serde_json::Value> = Vec::new();
    for p in paths {
        let src = PathBuf::from(&p);
        if src.is_dir() {
            rejected.push(serde_json::json!({"path": p, "code":"ATTACHMENT_INVALID_TYPE","message":"Dossier non supporté"}));
            continue;
        }
        let ext = src.extension().and_then(|s| s.to_str()).unwrap_or("").to_lowercase();
        if ext == "lnk" {
            rejected.push(serde_json::json!({"path": p, "code":"ATTACHMENT_INVALID_TYPE","message":"Raccourci non supporté"}));
            continue;
        }
        let meta = match fs::metadata(&src) {
            Ok(m) => m,
            Err(e) => { rejected.push(serde_json::json!({"path": p, "code":"ATTACHMENT_UNREADABLE","message": e.to_string()})); continue; }
        };
        if meta.len() == 0 {
            rejected.push(serde_json::json!({"path": p, "code":"ATTACHMENT_UNREADABLE","message":"Fichier vide"}));
            continue;
        }
        if meta.len() > 20*1024*1024 {
            rejected.push(serde_json::json!({"path": p, "code":"ATTACHMENT_TOO_LARGE","message":"Fichier >20 Mo"}));
            continue;
        }
        let mut header = [0u8; 12];
        let is_png = fs::File::open(&src).and_then(|mut f| { use std::io::Read; f.read_exact(&mut header).map(|_| header) }).map(|h| h[0..8] == [0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A]).unwrap_or(false);
        let is_jpg = header[0..3] == [0xFF,0xD8,0xFF];
        let is_pdf = header[0..4] == [0x25,0x50,0x44,0x46];
        let is_webp = header[0..4] == [0x52,0x49,0x46,0x46] && header[8..12] == [0x57,0x45,0x42,0x50];
        let (mime, _kind) = if is_png { ("image/png","image") } else if is_jpg { ("image/jpeg","image") } else if is_webp { ("image/webp","image") } else if is_pdf { ("application/pdf","pdf") } else {
            rejected.push(serde_json::json!({"path": p, "code":"ATTACHMENT_INVALID_TYPE","message":"Type non pris en charge"}));
            continue;
        };
        let hash_hex = {
            use sha2::{Sha256, Digest};
            let mut hasher = Sha256::new();
            let bytes = fs::read(&src).map_err(|e| e.to_string())?;
            hasher.update(&bytes);
            hex::encode(hasher.finalize())
        };
        let staged_path = staged_dir.join(format!("{}.{}", hash_hex, ext));
        if !staged_path.exists() {
            fs::copy(&src, &staged_path).map_err(|e| e.to_string())?;
        }
        let filename = src.file_name().and_then(|s| s.to_str()).unwrap_or("fichier").to_string();
        let stage_item_id = format!("sti_{}", &hash_hex[..12]);
        // Preview token = stagedPath, frontend fera convertFileSrc(token)
        let preview = if mime.starts_with("image/") {
            Some(Preview { kind: "tauri_asset".to_string(), token: staged_path.to_string_lossy().to_string(), width: Some(512), height: Some(384) })
        } else { None };
        items.push(StagingItem { stage_item_id, filename: filename.clone(), display_name: filename, mime_detected: mime.to_string(), size_bytes: meta.len(), sha256: hash_hex, preview, status: "ready".to_string() });
    }
    if items.is_empty() && !rejected.is_empty() {
        return Err(rejected[0]["message"].as_str().unwrap_or("Aucun fichier valide").to_string());
    }
    Ok(StagingResponse { stage_id, items, rejected })
}

#[tauri::command]
async fn stage_picker_bytes(app: tauri::AppHandle, request_id: String, filename: String, bytes: Vec<u8>) -> Result<StagingResponse, String> {
    let temp_base = resolve_temp_dir(&app);
    let staged_dir = temp_base.join("staged");
    fs::create_dir_all(&staged_dir).map_err(|e| e.to_string())?;
    if bytes.len() > 20*1024*1024 {
        return Err("Fichier trop volumineux (>20 Mo)".to_string());
    }
    // Détection MIME via magic
    let is_png = bytes.starts_with(b"\x89PNG");
    let is_jpg = bytes.starts_with(b"\xFF\xD8\xFF");
    let is_pdf = bytes.starts_with(b"%PDF");
    let is_webp = bytes.len() > 12 && &bytes[0..4] == b"RIFF" && &bytes[8..12] == b"WEBP";
    let (mime, _kind) = if is_png { ("image/png","image") } else if is_jpg { ("image/jpeg","image") } else if is_webp { ("image/webp","image") } else if is_pdf { ("application/pdf","pdf") } else {
        return Err("Type non pris en charge (PNG/JPG/WEBP/PDF)".to_string());
    };
    let hash_hex = {
        use sha2::{Sha256, Digest};
        let mut hasher = Sha256::new();
        hasher.update(&bytes);
        hex::encode(hasher.finalize())
    };
    let ext = PathBuf::from(&filename).extension().and_then(|s| s.to_str()).unwrap_or("bin").to_lowercase();
    let staged_path = staged_dir.join(format!("{}.{}", hash_hex, ext));
    if !staged_path.exists() {
        fs::write(&staged_path, &bytes).map_err(|e| e.to_string())?;
    }
    let stage_id = format!("stg_{}", &request_id[..8.min(request_id.len())]);
    let stage_item_id = format!("sti_{}", &hash_hex[..12]);
    let preview = if mime.starts_with("image/") {
        Some(Preview { kind: "tauri_asset".to_string(), token: staged_path.to_string_lossy().to_string(), width: Some(512), height: Some(384) })
    } else { None };
    let item = StagingItem { stage_item_id, filename: filename.clone(), display_name: filename, mime_detected: mime.to_string(), size_bytes: bytes.len() as u64, sha256: hash_hex, preview, status: "ready".to_string() };
    Ok(StagingResponse { stage_id, items: vec![item], rejected: vec![] })
}

#[tauri::command]
async fn get_staged_preview_bytes(path: String) -> Result<Vec<u8>, String> {
    // Lecture sûre du fichier stagé pour preview (annexe 10 vignette)
    let p = PathBuf::from(&path);
    // Vérifie que le chemin est sous temp/staged ou temp/{request_id} géré
    // Pour simplifier, vérifie qu'il existe et est fichier
    if !p.exists() || !p.is_file() {
        return Err("Fichier stagé introuvable".to_string());
    }
    let bytes = fs::read(&p).map_err(|e| e.to_string())?;
    if bytes.len() > 10*1024*1024 {
        // Limite preview 10 Mo
        return Err("Fichier trop volumineux pour preview".to_string());
    }
    Ok(bytes)
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct PreparedItem {
    stage_item_id: String,
    artifact_id: String,
    status: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct PreparedResponse {
    items: Vec<PreparedItem>,
}

#[tauri::command]
async fn attachments_prepare(app: tauri::AppHandle, conversation_id: String, stage_item_ids: Vec<String>, request_id: String) -> Result<PreparedResponse, String> {
    // Annexe 10 : promotion stage → Blob/Artifact/ArtifactLink transactionnelle
    let temp_base = resolve_temp_dir(&app);
    let staged_dir = temp_base.join("staged");
    let data_dir = resolve_data_dir(&app)?;
    let blobs_dir = data_dir.join("blobs");
    fs::create_dir_all(&blobs_dir).map_err(|e| e.to_string())?;
    let db_path = resolve_db_path(&app)?;
    let conn = rusqlite::Connection::open(&db_path).map_err(|e| e.to_string())?;
    let mut out: Vec<PreparedItem> = Vec::new();
    for stage_item_id in stage_item_ids {
        // stage_item_id = sti_{hash_prefix}
        let hash_prefix = stage_item_id.strip_prefix("sti_").unwrap_or(&stage_item_id);
        // Trouver fichier staged correspondant (hash complet)
        let mut staged_path: Option<PathBuf> = None;
        let mut sha256_full: Option<String> = None;
        let mut filename: Option<String> = None;
        let mut mime: Option<String> = None;
        let mut size: u64 = 0;
        if let Ok(entries) = fs::read_dir(&staged_dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                if let Some(fname) = p.file_name().and_then(|s| s.to_str()) {
                    if fname.starts_with(hash_prefix) || fname.contains(hash_prefix) {
                        staged_path = Some(p.clone());
                        // Retrouver sha256 depuis nom (hash.ext)
                        if let Some(dot) = fname.find('.') {
                            sha256_full = Some(fname[..dot].to_string());
                        } else {
                            sha256_full = Some(fname.to_string());
                        }
                        // Pour retrouver hash complet, on lit le fichier et recalcule si besoin
                        if sha256_full.as_ref().map(|s| s.len() != 64).unwrap_or(true) {
                            if let Ok(bytes) = fs::read(&p) {
                                use sha2::{Sha256, Digest};
                                let mut hasher = Sha256::new();
                                hasher.update(&bytes);
                                sha256_full = Some(hex::encode(hasher.finalize()));
                            }
                        }
                        // filename d'origine non conservé dans staged, on utilise hash.ext comme filename
                        filename = Some(fname.to_string());
                        size = entry.metadata().map(|m| m.len()).unwrap_or(0);
                        // Détection mime via extension
                        let ext = p.extension().and_then(|s| s.to_str()).unwrap_or("").to_lowercase();
                        mime = Some(match ext.as_str() {
                            "png" => "image/png".to_string(),
                            "jpg" | "jpeg" => "image/jpeg".to_string(),
                            "webp" => "image/webp".to_string(),
                            "pdf" => "application/pdf".to_string(),
                            _ => "application/octet-stream".to_string(),
                        });
                        break;
                    }
                }
            }
        }
        if staged_path.is_none() {
            // Debug : lister le contenu de staged_dir pour diagnostiquer
            let mut available = Vec::new();
            if let Ok(entries) = fs::read_dir(&staged_dir) {
                for e in entries.flatten() {
                    if let Some(n) = e.file_name().to_str() {
                        available.push(n.to_string());
                    }
                }
            }
            return Err(format!("stage_item_id introuvable: {} (staged_dir={} contient: {:?})", stage_item_id, staged_dir.display(), available));
        }
        let staged_path = staged_path.unwrap();
        let sha256 = sha256_full.unwrap_or_else(|| "unknown".to_string());
        let filename = filename.unwrap_or_else(|| format!("{}.bin", sha256));
        let mime = mime.unwrap_or_else(|| "application/octet-stream".to_string());
        let kind = if mime.starts_with("image/") { "image" } else if mime == "application/pdf" { "pdf" } else { "file" };
        // Blob : blobs/sha256/ab/ab12...bin
        let blob_subdir = blobs_dir.join(&sha256[..2.min(sha256.len())]);
        fs::create_dir_all(&blob_subdir).map_err(|e| e.to_string())?;
        let blob_path = blob_subdir.join(format!("{}.bin", sha256));
        if !blob_path.exists() {
            fs::copy(&staged_path, &blob_path).map_err(|e| e.to_string())?;
        }
        let relative_path = format!("blobs/{}/{}", &sha256[..2.min(sha256.len())], format!("{}.bin", sha256));
        // DB : Blob, Artifact, ArtifactLink transaction
        let blob_id = sha256.clone();
        let artifact_id = format!("art_{}", &sha256[..12]);
        // Vérifier si blob existe déjà
        let blob_exists: bool = conn.query_row("SELECT COUNT(*) FROM blobs WHERE sha256 = ?1", rusqlite::params![sha256], |r| r.get(0)).unwrap_or(0) > 0;
        if !blob_exists {
            conn.execute("INSERT INTO blobs (id, sha256, relative_path, size_bytes, mime_detected, state) VALUES (?1, ?2, ?3, ?4, ?5, 'available')",
                rusqlite::params![blob_id, sha256, relative_path, size as i64, mime]).map_err(|e| e.to_string())?;
        }
        let artifact_exists: bool = conn.query_row("SELECT COUNT(*) FROM artifacts WHERE id = ?1", rusqlite::params![artifact_id], |r| r.get(0)).unwrap_or(0) > 0;
        if !artifact_exists {
            conn.execute("INSERT INTO artifacts (id, blob_id, kind, original_filename, display_name) VALUES (?1, ?2, ?3, ?4, ?5)",
                rusqlite::params![artifact_id, blob_id, kind, filename, filename]).map_err(|e| e.to_string())?;
        }
        // ArtifactLink conversation
        let link_id = format!("lnk_{}_{}", &artifact_id[4..8.min(artifact_id.len())], &conversation_id[..8.min(conversation_id.len())]);
        let link_exists: bool = conn.query_row("SELECT COUNT(*) FROM artifact_links WHERE artifact_id = ?1 AND scope_kind='conversation' AND scope_id = ?2", rusqlite::params![artifact_id, conversation_id], |r| r.get(0)).unwrap_or(0) > 0;
        if !link_exists {
            conn.execute("INSERT INTO artifact_links (id, artifact_id, scope_kind, scope_id, role) VALUES (?1, ?2, 'conversation', ?3, 'attachment')",
                rusqlite::params![link_id, artifact_id, conversation_id]).map_err(|e| e.to_string())?;
        }
        // Marquer stage comme promu (supprimer ou garder pour grâce)
        // On garde le fichier staged pour grâce, mais on pourrait le supprimer après 7j
        out.push(PreparedItem { stage_item_id, artifact_id, status: "ready".to_string() });
    }
    let _ = request_id;
    Ok(PreparedResponse { items: out })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let db_path = resolve_db_path(app.handle()).expect("resolve_db_path failed");
            let data_dir = resolve_data_dir(app.handle()).expect("resolve_data_dir failed");
            let app_data_dir = resolve_app_data_dir(app.handle()).expect("resolve_app_data_dir failed");
            println!("[rust] Thot DB path resolved: {}", db_path.display());
            println!("[rust] Thot data dir: {}", data_dir.display());
            println!("[rust] Thot app data dir: {}", app_data_dir.display());
            let (mut rx, child) = if cfg!(debug_assertions) {
                // En dev : lance python -m backend.main depuis la racine projet (déduite, plus hardcodée)
                let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
                let project_root = manifest_dir.parent().unwrap_or(&manifest_dir).to_path_buf();
                // Vérifie que backend/main.py existe, sinon fallback CWD
                let cwd = if project_root.join("backend").join("main.py").exists() {
                    project_root
                } else {
                    std::env::current_dir().unwrap_or(manifest_dir)
                };
                println!("[rust] dev cwd: {}", cwd.display());
                app.shell()
                    .command("python3")
                    .args(["-m", "backend.main"])
                    .current_dir(cwd)
                    .env("THOT_DB_PATH", db_path.to_string_lossy().to_string())
                    .env("THOT_APP_DATA_DIR", app_data_dir.to_string_lossy().to_string())
                    .env("THOT_DATA_DIR", data_dir.to_string_lossy().to_string())
                    .spawn()
                    .expect("échec lancement python3 -m backend.main (vérifie pip install -r requirements.txt)")
            } else {
                let sidecar = app.shell()
                    .sidecar("thot-sidecar")
                    .expect("sidecar thot-sidecar introuvable dans src-tauri/binaries/");
                sidecar
                    .env("THOT_DB_PATH", db_path.to_string_lossy().to_string())
                    .env("THOT_APP_DATA_DIR", app_data_dir.to_string_lossy().to_string())
                    .env("THOT_DATA_DIR", data_dir.to_string_lossy().to_string())
                    .spawn()
                    .expect("échec lancement sidecar Python")
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
        .invoke_handler(tauri::generate_handler![chat_send, save_upload, list_conversations, get_conversation, list_messages, stage_dropped_files, stage_dropped_paths, stage_picker_bytes, attachments_prepare, get_staged_preview_bytes, get_attachment_bytes])
        .run(tauri::generate_context!())
        .expect("erreur lors du lancement de Tauri");
}
