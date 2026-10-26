#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpListener;
use std::time::{Duration, Instant};

const AUTH_PREFIX: &str = "https://ycqczfrphjhsnelivact.supabase.co/auth/v1/oauth/authorize?";
const PORT: u16 = 53682;
const DONE_HTML: &str = "<!doctype html><meta charset=utf-8><title>Overlay Studio</title><body style=\"background:#0a0a0b;color:#f5f5f7;font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0\"><div style=\"text-align:center\"><h2>Готово</h2><p style=\"color:#a1a1aa\">Можно закрыть эту вкладку и вернуться в Overlay Studio.</p></div></body>";

/// Ждёт один запрос вида GET /callback?code=...&state=... на 127.0.0.1 и возвращает его query-строку.
fn wait_callback(listener: TcpListener, timeout: Duration) -> Result<String, String> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        match listener.accept() {
            Ok((mut stream, _)) => {
                let _ = stream.set_nonblocking(false);
                let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
                let mut buf = [0u8; 8192];
                let n = stream.read(&mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let path = req.lines().next().unwrap_or("").split_whitespace().nth(1).unwrap_or("");
                if let Some(q) = path.strip_prefix("/callback?") {
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n{}",
                        DONE_HTML.len(),
                        DONE_HTML
                    );
                    let _ = stream.write_all(resp.as_bytes());
                    return Ok(q.to_string());
                }
                let _ = stream.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => std::thread::sleep(Duration::from_millis(100)),
            Err(e) => return Err(e.to_string()),
        }
    }
    Err("время ожидания входа истекло".to_string())
}

#[tauri::command]
async fn browser_login(url: String) -> Result<String, String> {
    if !url.starts_with(AUTH_PREFIX) {
        return Err("недопустимый адрес".into());
    }
    let listener = TcpListener::bind(("127.0.0.1", PORT)).map_err(|e| format!("порт {} занят: {}", PORT, e))?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    webbrowser::open(&url).map_err(|e| format!("не удалось открыть браузер: {}", e))?;
    tauri::async_runtime::spawn_blocking(move || wait_callback(listener, Duration::from_secs(300)))
        .await
        .map_err(|e| e.to_string())?
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![browser_login])
        .run(tauri::generate_context!())
        .expect("не удалось запустить Overlay Studio");
}
