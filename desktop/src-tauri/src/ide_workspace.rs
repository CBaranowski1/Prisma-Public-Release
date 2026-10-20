//! Native, workspace-confined file access for Prisma's integrated Model IDE.

use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use tauri::State;

use crate::orchestrator::AppState;

const MAX_FILE_BYTES: u64 = 4 * 1024 * 1024;
const MAX_TREE_ENTRIES: usize = 5_000;
const MAX_TREE_DEPTH: usize = 16;
const IGNORED_DIRS: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "target",
    "venv",
    ".venv",
    "dist",
    "build",
];

#[derive(Debug, Clone, Serialize)]
pub struct WorkspaceInfo {
    pub root: String,
    pub name: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct TreeEntry {
    pub path: String,
    pub name: String,
    pub is_dir: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct FileSnapshot {
    pub path: String,
    pub content: String,
    pub revision: String,
    pub language: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct FileRevision {
    pub path: String,
    pub revision: String,
}

fn workspace_config_path() -> PathBuf {
    let base = std::env::var("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir());
    base.join("Prisma").join("model-ide-workspace.txt")
}

pub fn load_saved_workspace() -> Option<PathBuf> {
    let raw = fs::read_to_string(workspace_config_path()).ok()?;
    canonical_workspace(Path::new(raw.trim())).ok()
}

fn save_workspace(root: &Path) -> Result<(), String> {
    let path = workspace_config_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    fs::write(path, root.to_string_lossy().as_bytes()).map_err(|error| error.to_string())
}

fn canonical_workspace(path: &Path) -> Result<PathBuf, String> {
    let canonical = path
        .canonicalize()
        .map_err(|error| format!("Cannot open workspace: {error}"))?;
    if !canonical.is_dir() {
        return Err("The selected workspace is not a directory".into());
    }
    Ok(canonical)
}

fn workspace_info(root: &Path) -> WorkspaceInfo {
    WorkspaceInfo {
        root: root.to_string_lossy().into_owned(),
        name: root
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("Workspace")
            .to_owned(),
    }
}

fn current_root(state: &AppState) -> Result<PathBuf, String> {
    state
        .ide_workspace
        .lock()
        .map_err(|_| "Workspace lock was poisoned".to_string())?
        .clone()
        .ok_or_else(|| "Choose a workspace first".to_string())
}

fn set_root(state: &AppState, path: &Path) -> Result<WorkspaceInfo, String> {
    let root = canonical_workspace(path)?;
    save_workspace(&root)?;
    *state
        .ide_workspace
        .lock()
        .map_err(|_| "Workspace lock was poisoned".to_string())? = Some(root.clone());
    Ok(workspace_info(&root))
}

fn validate_relative_path(relative: &str) -> Result<PathBuf, String> {
    let path = Path::new(relative);
    if relative.trim().is_empty() || path.is_absolute() {
        return Err("A workspace-relative path is required".into());
    }
    for component in path.components() {
        if matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        ) {
            return Err("The path leaves the selected workspace".into());
        }
    }
    Ok(path.to_path_buf())
}

fn existing_ancestor(path: &Path) -> Option<&Path> {
    let mut cursor = path;
    loop {
        if cursor.exists() {
            return Some(cursor);
        }
        cursor = cursor.parent()?;
    }
}

fn confined_path(root: &Path, relative: &str, allow_new: bool) -> Result<PathBuf, String> {
    let relative = validate_relative_path(relative)?;
    let candidate = root.join(relative);
    let check = if candidate.exists() {
        candidate
            .canonicalize()
            .map_err(|error| format!("Cannot resolve file: {error}"))?
    } else if allow_new {
        existing_ancestor(&candidate)
            .ok_or_else(|| "Cannot resolve the file's parent directory".to_string())?
            .canonicalize()
            .map_err(|error| format!("Cannot resolve file parent: {error}"))?
    } else {
        return Err("File does not exist".into());
    };
    if !check.starts_with(root) {
        return Err("The path resolves outside the selected workspace".into());
    }
    Ok(candidate)
}

fn revision(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn language_for(path: &Path) -> String {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "js" | "mjs" | "cjs" => "javascript",
        "ts" | "tsx" => "typescript",
        "jsx" => "jsx",
        "py" => "python",
        "rs" => "rust",
        "json" => "json",
        "html" | "htm" => "html",
        "css" | "scss" | "less" => "css",
        "sql" => "sql",
        "md" | "markdown" => "markdown",
        "yaml" | "yml" => "yaml",
        "toml" => "toml",
        "sh" | "bash" | "ps1" => "shell",
        "go" => "go",
        "java" => "java",
        "c" | "h" | "cpp" | "hpp" | "cc" => "cpp",
        _ => "text",
    }
    .to_owned()
}

fn relative_display(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn collect_tree(
    root: &Path,
    directory: &Path,
    depth: usize,
    entries: &mut Vec<TreeEntry>,
) -> Result<(), String> {
    if depth > MAX_TREE_DEPTH || entries.len() >= MAX_TREE_ENTRIES {
        return Ok(());
    }
    let mut children = fs::read_dir(directory)
        .map_err(|error| format!("Cannot list workspace: {error}"))?
        .filter_map(Result::ok)
        .collect::<Vec<_>>();
    children.sort_by_key(|entry| {
        let is_file = entry.file_type().map(|kind| kind.is_file()).unwrap_or(true);
        (
            is_file,
            entry.file_name().to_string_lossy().to_ascii_lowercase(),
        )
    });

    for child in children {
        if entries.len() >= MAX_TREE_ENTRIES {
            break;
        }
        let file_type = match child.file_type() {
            Ok(value) => value,
            Err(_) => continue,
        };
        if file_type.is_symlink() {
            continue;
        }
        let name = child.file_name().to_string_lossy().into_owned();
        if file_type.is_dir() && IGNORED_DIRS.contains(&name.as_str()) {
            continue;
        }
        let path = child.path();
        let is_dir = file_type.is_dir();
        entries.push(TreeEntry {
            path: relative_display(root, &path),
            name,
            is_dir,
        });
        if is_dir {
            collect_tree(root, &path, depth + 1, entries)?;
        }
    }
    Ok(())
}

#[tauri::command]
pub fn ide_get_workspace(state: State<AppState>) -> Result<Option<WorkspaceInfo>, String> {
    let guard = state
        .ide_workspace
        .lock()
        .map_err(|_| "Workspace lock was poisoned".to_string())?;
    Ok(guard.as_deref().map(workspace_info))
}

#[tauri::command]
pub fn ide_choose_workspace(state: State<AppState>) -> Result<Option<WorkspaceInfo>, String> {
    let selected = rfd::FileDialog::new()
        .set_title("Choose a Prisma Model IDE workspace")
        .pick_folder();
    selected
        .as_deref()
        .map(|path| set_root(&state, path))
        .transpose()
}

#[tauri::command]
pub fn ide_list_tree(state: State<AppState>) -> Result<Vec<TreeEntry>, String> {
    let root = current_root(&state)?;
    let mut entries = Vec::new();
    collect_tree(&root, &root, 0, &mut entries)?;
    Ok(entries)
}

#[tauri::command]
pub fn ide_read_file(state: State<AppState>, path: String) -> Result<FileSnapshot, String> {
    let root = current_root(&state)?;
    let target = confined_path(&root, &path, false)?;
    let metadata = fs::metadata(&target).map_err(|error| error.to_string())?;
    if !metadata.is_file() {
        return Err("The selected path is not a file".into());
    }
    if metadata.len() > MAX_FILE_BYTES {
        return Err("This file is larger than the 4 MB editor safety limit".into());
    }
    let bytes = fs::read(&target).map_err(|error| format!("Cannot read file: {error}"))?;
    if bytes.contains(&0) {
        return Err("Binary files cannot be opened in the code editor".into());
    }
    let content =
        String::from_utf8(bytes.clone()).map_err(|_| "The file is not valid UTF-8".to_string())?;
    Ok(FileSnapshot {
        path: relative_display(&root, &target),
        content,
        revision: revision(&bytes),
        language: language_for(&target),
    })
}

#[tauri::command]
pub fn ide_stat_file(state: State<AppState>, path: String) -> Result<FileRevision, String> {
    let root = current_root(&state)?;
    let target = confined_path(&root, &path, false)?;
    let bytes = fs::read(&target).map_err(|error| format!("Cannot read file: {error}"))?;
    Ok(FileRevision {
        path: relative_display(&root, &target),
        revision: revision(&bytes),
    })
}

#[tauri::command]
pub fn ide_write_file(
    state: State<AppState>,
    path: String,
    content: String,
    expected_revision: Option<String>,
) -> Result<FileRevision, String> {
    if content.len() as u64 > MAX_FILE_BYTES {
        return Err("The edited file exceeds the 4 MB safety limit".into());
    }
    let root = current_root(&state)?;
    let target = confined_path(&root, &path, true)?;

    if target.exists() {
        let current = fs::read(&target).map_err(|error| format!("Cannot read file: {error}"))?;
        let expected = expected_revision
            .as_deref()
            .ok_or_else(|| "Open the existing file before overwriting it".to_string())?;
        if revision(&current) != expected {
            return Err(
                "The file changed outside Prisma. Reload it before saving your edits.".into(),
            );
        }
    } else if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("Cannot create the file directory: {error}"))?;
    }

    let mut file = fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&target)
        .map_err(|error| format!("Cannot save file: {error}"))?;
    file.write_all(content.as_bytes())
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("Cannot save file: {error}"))?;

    Ok(FileRevision {
        path: relative_display(&root, &target),
        revision: revision(content.as_bytes()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_paths_that_leave_workspace() {
        assert!(validate_relative_path("../secret.txt").is_err());
        assert!(validate_relative_path("src/../../secret.txt").is_err());
        assert!(validate_relative_path("src/main.rs").is_ok());
    }

    #[test]
    fn revisions_change_with_content() {
        assert_ne!(revision(b"one"), revision(b"two"));
        assert_eq!(revision(b"same"), revision(b"same"));
    }

    #[test]
    fn language_detection_covers_primary_ide_types() {
        assert_eq!(language_for(Path::new("app.py")), "python");
        assert_eq!(language_for(Path::new("main.ts")), "typescript");
        assert_eq!(language_for(Path::new("schema.sql")), "sql");
        assert_eq!(language_for(Path::new("README.md")), "markdown");
    }
}
