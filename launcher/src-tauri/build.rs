fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["browser_login"])),
    )
    .expect("не удалось выполнить tauri-build");
}
