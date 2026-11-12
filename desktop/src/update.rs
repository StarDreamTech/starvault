//! Optional updates from the public StarDreamTech/starvault stable releases.
//! The client never reads or sends a token, and it never contacts another repository.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

pub const RELEASE_OWNER: &str = "StarDreamTech";
pub const RELEASE_REPO: &str = "starvault";
pub const LATEST_RELEASE_URL: &str =
    "https://api.github.com/repos/StarDreamTech/starvault/releases/latest";
pub const RELEASES_PAGE: &str = "https://github.com/StarDreamTech/starvault/releases";
/// Minisign public key (the base64 line only). Empty until a release signing key is published.
/// This is a public key, never a secret.
pub const RELEASE_MINISIGN_PUBLIC_KEY: &str = "";

const USER_AGENT: &str = concat!("StarVault/", env!("CARGO_PKG_VERSION"), " (desktop)");
const API_LIMIT: u64 = 2 * 1024 * 1024;
const SUMS_LIMIT: u64 = 1024 * 1024;
const SIG_LIMIT: u64 = 64 * 1024;
const PACKAGE_LIMIT: u64 = 256 * 1024 * 1024;
const SUMS_NAME: &str = "SHA256SUMS";
const SUMS_SIGNATURE: &str = "SHA256SUMS.minisig";
const WINDOWS_EXE: &str = "chat-history-vault.exe";
const CANCELLED: &str = "已取消安装";

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Version {
    major: u64,
    minor: u64,
    patch: u64,
}

impl std::fmt::Display for Version {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}.{}.{}", self.major, self.minor, self.patch)
    }
}

pub fn current_version() -> Version {
    parse_version(env!("CARGO_PKG_VERSION")).expect("package version is stable")
}

/// Stable `X.Y.Z` or `vX.Y.Z` only. Pre-releases and other text are ignored.
pub fn parse_version(text: &str) -> Option<Version> {
    let text = text.trim();
    let text = text
        .strip_prefix('v')
        .or_else(|| text.strip_prefix('V'))
        .unwrap_or(text);
    if text.is_empty() || text.contains('-') || text.contains('+') {
        return None;
    }
    let mut parts = text.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    let patch = parts.next()?.parse().ok()?;
    if parts.next().is_some() {
        return None;
    }
    Some(Version {
        major,
        minor,
        patch,
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    MacosArm64,
    MacosX64,
    WindowsX64,
    WindowsArm64,
    Linux,
    Other,
}

pub fn current_platform() -> Platform {
    if cfg!(target_os = "macos") && cfg!(target_arch = "aarch64") {
        Platform::MacosArm64
    } else if cfg!(target_os = "macos") && cfg!(target_arch = "x86_64") {
        Platform::MacosX64
    } else if cfg!(target_os = "windows") && cfg!(target_arch = "x86_64") {
        Platform::WindowsX64
    } else if cfg!(target_os = "windows") && cfg!(target_arch = "aarch64") {
        Platform::WindowsArm64
    } else if cfg!(target_os = "linux") {
        Platform::Linux
    } else {
        Platform::Other
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackageRef {
    pub name: String,
    pub url: String,
    pub sums_url: Option<String>,
    pub file_signature_url: Option<String>,
    pub sums_signature_url: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OfferAction {
    Swap(PackageRef),
    Manual,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Offer {
    pub version: Version,
    pub page: String,
    pub summary: String,
    pub action: OfferAction,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum UpdateReport {
    UpToDate(Version),
    Available(Offer),
    Unavailable,
}

#[derive(Debug)]
pub enum UiDecision {
    Silent,
    Note(String),
    Offer(Offer),
}

/// Automatic checks stay silent when nothing new is confirmed. A manual check may show a short note.
pub fn present(manual: bool, report: UpdateReport) -> UiDecision {
    match report {
        UpdateReport::Available(offer) => UiDecision::Offer(offer),
        UpdateReport::UpToDate(version) if manual => {
            UiDecision::Note(format!("已是最新版本 {version}。"))
        }
        UpdateReport::Unavailable if manual => {
            UiDecision::Note("暂时无法检查更新。当前使用不受影响。".into())
        }
        UpdateReport::UpToDate(_) | UpdateReport::Unavailable => UiDecision::Silent,
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SourceError {
    Network,
    ForbiddenHost,
    TooLarge,
}

pub trait ReleaseSource {
    fn get(&self, url: &str, limit: u64) -> Result<Vec<u8>, SourceError>;
}

pub struct CurlSource;

impl ReleaseSource for CurlSource {
    fn get(&self, url: &str, limit: u64) -> Result<Vec<u8>, SourceError> {
        if validate_initial_url(url).is_err() {
            return Err(SourceError::ForbiddenHost);
        }
        let curl = curl_program().ok_or(SourceError::Network)?;
        let path = temp_download_path();
        let _guard = TempFile(&path);
        let output = Command::new(curl)
            .args(curl_args(url, &path, limit))
            .stdin(Stdio::null())
            .output()
            .map_err(|_| SourceError::Network)?;
        if !output.status.success() {
            return Err(SourceError::Network);
        }
        let effective = String::from_utf8_lossy(&output.stdout);
        validate_effective_url(effective.trim())?;
        let bytes = fs::read(&path).map_err(|_| SourceError::Network)?;
        if bytes.len() as u64 > limit {
            return Err(SourceError::TooLarge);
        }
        Ok(bytes)
    }
}

pub fn check_latest(
    source: &dyn ReleaseSource,
    current: Version,
    platform: Platform,
    exe: &Path,
) -> UpdateReport {
    if validate_initial_url(LATEST_RELEASE_URL).is_err() {
        return UpdateReport::Unavailable;
    }
    let bytes = match source.get(LATEST_RELEASE_URL, API_LIMIT) {
        Ok(bytes) => bytes,
        Err(_) => return UpdateReport::Unavailable,
    };
    let Ok(text) = String::from_utf8(bytes) else {
        return UpdateReport::Unavailable;
    };
    interpret_release(&text, current, platform, exe, RELEASE_MINISIGN_PUBLIC_KEY)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SwapKind {
    MacosApp,
    WindowsDir,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PreparedSwap {
    pub kind: SwapKind,
    pub script: PathBuf,
    pub log: PathBuf,
    pub zip: PathBuf,
    pub target: PathBuf,
}

pub fn prepare_swap(
    source: &dyn ReleaseSource,
    package: &PackageRef,
    platform: Platform,
    exe: &Path,
    dir: &Path,
    public_key: &str,
) -> Result<PreparedSwap, String> {
    let kind = installed_target(platform, exe).map_err(str::to_string)?;
    if !name_matches(platform, &package.name) || !safe_token(&package.name) {
        return Err(format!("安装包名称无法确认，{CANCELLED}。"));
    }
    for url in package_urls(package) {
        if validate_initial_url(url).is_err() {
            return Err(host_rejected());
        }
    }
    fs::create_dir_all(dir).map_err(|_| "无法准备更新目录，已取消安装。".to_string())?;

    let key_ready = !public_key.trim().is_empty();
    let file_sig_url = if key_ready {
        package.file_signature_url.as_deref()
    } else {
        None
    };
    let file_signature = download_text(source, file_sig_url, SIG_LIMIT, file_sig_url.is_some())?;
    let sums_sig_url = if key_ready && file_signature.is_none() {
        package.sums_signature_url.as_deref()
    } else {
        None
    };
    let sums_signature = download_text(source, sums_sig_url, SIG_LIMIT, sums_sig_url.is_some())?;
    let sums = download_text(
        source,
        package.sums_url.as_deref(),
        SUMS_LIMIT,
        file_signature.is_none(),
    )?;
    let file = match source.get(&package.url, PACKAGE_LIMIT) {
        Ok(file) => file,
        Err(SourceError::ForbiddenHost) => return Err(host_rejected()),
        Err(SourceError::TooLarge) => return Err(format!("安装包超出大小限制，{CANCELLED}。")),
        Err(SourceError::Network) => return Err(download_failed()),
    };
    verify_material(VerifyMaterial {
        filename: &package.name,
        file: &file,
        sums: sums.as_deref(),
        file_signature: file_signature.as_deref(),
        sums_signature: sums_signature.as_deref(),
        public_key,
    })?;
    let names = zip_names(&file).map_err(|_| archive_rejected())?;
    validate_archive(platform, &names)?;

    let zip = dir.join(&package.name);
    let script = dir.join(match kind {
        SwapKind::MacosApp => "replace.sh",
        SwapKind::WindowsDir => "replace.ps1",
    });
    let log = dir.join("replace.log");
    let target = match kind {
        SwapKind::MacosApp => {
            macos_app(exe).ok_or_else(|| "没有找到 StarVault.app，已取消安装。".to_string())?
        }
        SwapKind::WindowsDir => {
            windows_dir(exe).ok_or_else(|| "没有找到可替换的程序目录，已取消安装。".to_string())?
        }
    };
    if write_new(&zip, &file).is_err() {
        let _ = fs::remove_file(&zip);
        return Err("无法保存安装包，已取消安装。".into());
    }
    let body = match kind {
        SwapKind::MacosApp => MACOS_SCRIPT,
        SwapKind::WindowsDir => WINDOWS_SCRIPT,
    };
    if write_script(&script, body).is_err() {
        let _ = fs::remove_file(&zip);
        let _ = fs::remove_file(&script);
        return Err("无法准备替换程序，已取消安装。".into());
    }
    Ok(PreparedSwap {
        kind,
        script,
        log,
        zip,
        target,
    })
}

pub fn launch_swap(prepared: &PreparedSwap) -> Result<(), String> {
    let pid = std::process::id();
    let mut command = match prepared.kind {
        SwapKind::MacosApp => {
            let mut command = Command::new("/bin/bash");
            command.args(macos_args(pid, prepared));
            command
        }
        SwapKind::WindowsDir => {
            let mut command = Command::new(windows_powershell());
            command.args(windows_args(pid, prepared));
            command
        }
    };
    spawn_detached(&mut command)
}

pub fn macos_args(pid: u32, prepared: &PreparedSwap) -> Vec<String> {
    vec![
        prepared.script.display().to_string(),
        pid.to_string(),
        prepared.target.display().to_string(),
        prepared.zip.display().to_string(),
        prepared.log.display().to_string(),
    ]
}

pub fn windows_args(pid: u32, prepared: &PreparedSwap) -> Vec<String> {
    vec![
        "-NoProfile".into(),
        "-NonInteractive".into(),
        "-ExecutionPolicy".into(),
        "Bypass".into(),
        "-File".into(),
        prepared.script.display().to_string(),
        "-ProcessId".into(),
        pid.to_string(),
        "-InstallDir".into(),
        prepared.target.display().to_string(),
        "-ZipPath".into(),
        prepared.zip.display().to_string(),
        "-ExeName".into(),
        WINDOWS_EXE.into(),
        "-LogPath".into(),
        prepared.log.display().to_string(),
    ]
}

pub fn is_releases_page(value: &str) -> bool {
    let Ok(url) = url::Url::parse(value) else {
        return false;
    };
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.host_str() != Some("github.com")
    {
        return false;
    }
    let path = url.path().trim_end_matches('/');
    if path == "/StarDreamTech/starvault/releases" {
        return true;
    }
    let Some(tag) = path.strip_prefix("/StarDreamTech/starvault/releases/tag/") else {
        return false;
    };
    safe_token(tag) && tag.starts_with('v')
}

struct VerifyMaterial<'a> {
    filename: &'a str,
    file: &'a [u8],
    sums: Option<&'a str>,
    file_signature: Option<&'a str>,
    sums_signature: Option<&'a str>,
    public_key: &'a str,
}

fn verify_material(material: VerifyMaterial<'_>) -> Result<&'static str, String> {
    if !material.public_key.trim().is_empty() {
        if let Some(signature) = material.file_signature {
            verify_minisign(material.public_key, material.file, signature)?;
            if let Some(sums) = material.sums {
                check_hash(material.filename, material.file, sums)?;
                return Ok("minisign+sha256");
            }
            return Ok("minisign");
        }
        if let Some(signature) = material.sums_signature {
            let Some(sums) = material.sums else {
                return Err(format!("发布签名存在但缺少 SHA256SUMS，{CANCELLED}。"));
            };
            verify_minisign(material.public_key, sums.as_bytes(), signature)?;
            check_hash(material.filename, material.file, sums)?;
            return Ok("minisign+sha256");
        }
    }
    match material.sums {
        Some(sums) => {
            check_hash(material.filename, material.file, sums)?;
            Ok("sha256")
        }
        None => Err(format!("发布里没有签名，也没有 SHA256SUMS，{CANCELLED}。")),
    }
}

fn verify_minisign(public_key: &str, message: &[u8], signature_text: &str) -> Result<(), String> {
    let key = minisign_verify::PublicKey::from_base64(public_key.trim())
        .or_else(|_| minisign_verify::PublicKey::decode(public_key))
        .map_err(|_| signature_rejected())?;
    let signature =
        minisign_verify::Signature::decode(signature_text).map_err(|_| signature_rejected())?;
    key.verify(message, &signature, false)
        .map_err(|_| signature_rejected())
}

fn check_hash(filename: &str, file: &[u8], sums: &str) -> Result<(), String> {
    let expected = checksum_for(sums, filename)?;
    let actual = sha256_hex(file);
    if !hashes_equal(&actual, expected) {
        return Err(format!("SHA256 校验未通过，{CANCELLED}。"));
    }
    Ok(())
}

fn checksum_for<'a>(text: &'a str, filename: &str) -> Result<&'a str, String> {
    let mut found = None;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        let Some((hash, name)) = line.split_once(' ') else {
            return Err(format!("SHA256SUMS 无法读取，{CANCELLED}。"));
        };
        let name = name.trim().trim_start_matches('*');
        if hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(format!("SHA256SUMS 无法读取，{CANCELLED}。"));
        }
        if name.contains('/') || name.contains('\\') || name.contains("..") || name.is_empty() {
            return Err(format!("SHA256SUMS 含有非法文件名，{CANCELLED}。"));
        }
        if name == filename {
            if found.is_some() {
                return Err(format!(
                    "SHA256SUMS 对同一文件给出了多条校验，{CANCELLED}。"
                ));
            }
            found = Some(hash);
        }
    }
    found.ok_or_else(|| format!("SHA256SUMS 没有这一份安装包，{CANCELLED}。"))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(digest.len() * 2);
    for byte in digest {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0xf) as usize] as char);
    }
    out
}

fn hashes_equal(left: &str, right: &str) -> bool {
    let (Ok(left), Ok(right)) = (decode_hex(left), decode_hex(right)) else {
        return false;
    };
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0u8;
    for (a, b) in left.iter().zip(right.iter()) {
        diff |= a ^ b;
    }
    diff == 0
}

fn decode_hex(text: &str) -> Result<Vec<u8>, ()> {
    if text.len() % 2 != 0 {
        return Err(());
    }
    let mut out = Vec::with_capacity(text.len() / 2);
    let bytes = text.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        let high = hex_value(bytes[index])?;
        let low = hex_value(bytes[index + 1])?;
        out.push((high << 4) | low);
        index += 2;
    }
    Ok(out)
}

fn hex_value(byte: u8) -> Result<u8, ()> {
    match byte {
        b'0'..=b'9' => Ok(byte - b'0'),
        b'a'..=b'f' => Ok(byte - b'a' + 10),
        b'A'..=b'F' => Ok(byte - b'A' + 10),
        _ => Err(()),
    }
}

#[derive(Deserialize)]
struct ApiRelease {
    tag_name: String,
    prerelease: bool,
    draft: bool,
    assets: Vec<ApiAsset>,
}

#[derive(Deserialize)]
struct ApiAsset {
    name: String,
    #[serde(default)]
    state: String,
    #[serde(default)]
    size: Option<u64>,
}

fn interpret_release(
    body: &str,
    current: Version,
    platform: Platform,
    exe: &Path,
    public_key: &str,
) -> UpdateReport {
    let body = body.trim_start_matches('\u{feff}').trim();
    let Ok(release) = serde_json::from_str::<ApiRelease>(body) else {
        return UpdateReport::Unavailable;
    };
    if release.draft || release.prerelease {
        return UpdateReport::Unavailable;
    }
    let Some(version) = parse_version(&release.tag_name) else {
        return UpdateReport::Unavailable;
    };
    if release.tag_name != format!("v{version}") {
        return UpdateReport::Unavailable;
    }
    if version <= current {
        return UpdateReport::UpToDate(current);
    }
    let page = release_page(&release.tag_name);
    let manual =
        |reason: &str| UpdateReport::Available(manual_offer(version, page.clone(), reason));
    let Some(name) = package_name(platform, &version) else {
        return manual(platform_manual_reason(platform));
    };
    let Some(asset) = release
        .assets
        .iter()
        .find(|asset| asset.name == name && asset_ready(asset))
    else {
        return manual(missing_package_reason(platform));
    };
    if asset
        .size
        .is_some_and(|size| size == 0 || size > PACKAGE_LIMIT)
    {
        return manual("安装包超出自动更新大小限制，请前往 Releases 手动下载。");
    }
    let sums_ready = release
        .assets
        .iter()
        .any(|asset| asset.name == SUMS_NAME && asset_ready(asset));
    let file_sig_name = format!("{name}.minisig");
    let file_sig = release
        .assets
        .iter()
        .any(|asset| asset.name == file_sig_name && asset_ready(asset));
    let sums_sig = release
        .assets
        .iter()
        .any(|asset| asset.name == SUMS_SIGNATURE && asset_ready(asset));
    let signature_usable = !public_key.trim().is_empty() && (file_sig || sums_sig);
    if !sums_ready && !signature_usable {
        return manual("这一版没有可用的发布签名或 SHA256SUMS，请前往 Releases 手动下载。");
    }
    let Ok(url) = official_download_url(&release.tag_name, &name) else {
        return UpdateReport::Unavailable;
    };
    let sums_url = if sums_ready {
        match official_download_url(&release.tag_name, SUMS_NAME) {
            Ok(url) => Some(url),
            Err(()) => return UpdateReport::Unavailable,
        }
    } else {
        None
    };
    let file_signature_url = if file_sig {
        official_download_url(&release.tag_name, &file_sig_name).ok()
    } else {
        None
    };
    let sums_signature_url = if sums_sig {
        official_download_url(&release.tag_name, SUMS_SIGNATURE).ok()
    } else {
        None
    };
    if file_signature_url.is_none() && sums_signature_url.is_none() && sums_url.is_none() {
        return UpdateReport::Unavailable;
    }
    match installed_target(platform, exe) {
        Ok(_) => UpdateReport::Available(Offer {
            version,
            page,
            summary: format!(
                "有新版本 {version}。确认后才会下载；校验通过才会退出，替换旧版本并重新打开。"
            ),
            action: OfferAction::Swap(PackageRef {
                name,
                url,
                sums_url,
                file_signature_url,
                sums_signature_url,
            }),
        }),
        Err(reason) => manual(reason),
    }
}

fn manual_offer(version: Version, page: String, reason: &str) -> Offer {
    Offer {
        version,
        page,
        summary: format!("有新版本 {version}。{reason}"),
        action: OfferAction::Manual,
    }
}

fn package_name(platform: Platform, version: &Version) -> Option<String> {
    let suffix = match platform {
        Platform::MacosArm64 => "macos-arm64.zip",
        Platform::WindowsX64 => "windows-x64.zip",
        _ => return None,
    };
    Some(format!("StarVault-{version}-{suffix}"))
}

fn name_matches(platform: Platform, name: &str) -> bool {
    parse_name_version(name)
        .and_then(|version| package_name(platform, &version))
        .as_deref()
        == Some(name)
}

fn parse_name_version(name: &str) -> Option<Version> {
    let rest = name.strip_prefix("StarVault-")?;
    let rest = rest
        .strip_suffix("-macos-arm64.zip")
        .or_else(|| rest.strip_suffix("-windows-x64.zip"))?;
    parse_version(rest)
}

fn platform_manual_reason(platform: Platform) -> &'static str {
    match platform {
        Platform::Linux => "Linux 暂不支持在应用内替换安装，请前往 Releases 手动下载。",
        Platform::MacosX64 => "Intel 版 macOS 暂不能在应用内替换，请前往 Releases 手动下载。",
        _ => "当前系统暂不能在应用内替换，请前往 Releases 手动下载。",
    }
}

fn missing_package_reason(platform: Platform) -> &'static str {
    match platform {
        Platform::WindowsX64 => "这一版还没有 Windows 安装包，请前往 Releases 手动下载。",
        Platform::MacosArm64 => "这一版没有 macOS 安装包，请前往 Releases 手动下载。",
        other => platform_manual_reason(other),
    }
}

fn asset_ready(asset: &ApiAsset) -> bool {
    (asset.state.is_empty() || asset.state == "uploaded") && asset.size != Some(0)
}

fn installed_target(platform: Platform, exe: &Path) -> Result<SwapKind, &'static str> {
    match platform {
        Platform::MacosArm64 => {
            if macos_app(exe).is_some() {
                Ok(SwapKind::MacosApp)
            } else {
                Err("当前不是已安装的 StarVault.app，请前往 Releases 手动下载。")
            }
        }
        Platform::WindowsX64 => {
            if is_cargo_target(exe) {
                Err("当前是源码运行，请前往 Releases 手动下载 Windows 安装包。")
            } else if windows_dir(exe).is_some() {
                Ok(SwapKind::WindowsDir)
            } else {
                Err("没有找到可替换的 Windows 程序，请前往 Releases 手动下载。")
            }
        }
        other => Err(platform_manual_reason(other)),
    }
}

fn macos_app(exe: &Path) -> Option<PathBuf> {
    let text = exe.to_string_lossy();
    let absolute = text.starts_with('/');
    let parts: Vec<&str> = text.split('/').filter(|part| !part.is_empty()).collect();
    if parts.len() < 4 {
        return None;
    }
    let end = parts.len();
    if parts[end - 1] != "chat-history-vault"
        || parts[end - 2] != "MacOS"
        || parts[end - 3] != "Contents"
        || parts[end - 4] != "StarVault.app"
    {
        return None;
    }
    let joined = parts[..end - 3].join("/");
    let prefix = if absolute { "/" } else { "" };
    Some(PathBuf::from(format!("{prefix}{joined}")))
}

fn windows_dir(exe: &Path) -> Option<PathBuf> {
    let text = exe.to_string_lossy();
    let name = text.rsplit(['/', '\\']).next().unwrap_or("");
    if name != WINDOWS_EXE || is_cargo_target(exe) {
        return None;
    }
    let index = text.rfind(['/', '\\'])?;
    let parent = &text[..index];
    if parent.is_empty() {
        return None;
    }
    Some(PathBuf::from(parent.to_string()))
}

fn is_cargo_target(path: &Path) -> bool {
    let text = path
        .to_string_lossy()
        .replace('\\', "/")
        .to_ascii_lowercase();
    text.contains("/target/release/") || text.contains("/target/debug/")
}

fn official_download_url(tag: &str, name: &str) -> Result<String, ()> {
    if !safe_token(tag) || !tag.starts_with('v') || !safe_token(name) {
        return Err(());
    }
    Ok(format!(
        "https://github.com/{RELEASE_OWNER}/{RELEASE_REPO}/releases/download/{tag}/{name}"
    ))
}

fn release_page(tag: &str) -> String {
    let page = format!("https://github.com/{RELEASE_OWNER}/{RELEASE_REPO}/releases/tag/{tag}");
    if is_releases_page(&page) {
        page
    } else {
        RELEASES_PAGE.into()
    }
}

fn safe_token(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn validate_initial_url(value: &str) -> Result<(), SourceError> {
    let url = url::Url::parse(value).map_err(|_| SourceError::ForbiddenHost)?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path().contains("..")
    {
        return Err(SourceError::ForbiddenHost);
    }
    match url.host_str() {
        Some("api.github.com")
            if url.path() == "/repos/StarDreamTech/starvault/releases/latest" =>
        {
            Ok(())
        }
        Some("github.com") => {
            let Some(rest) = url
                .path()
                .strip_prefix("/StarDreamTech/starvault/releases/download/")
            else {
                return Err(SourceError::ForbiddenHost);
            };
            let mut parts = rest.split('/');
            let tag = parts.next().unwrap_or("");
            let name = parts.next().unwrap_or("");
            if parts.next().is_some()
                || !safe_token(tag)
                || !tag.starts_with('v')
                || !safe_token(name)
            {
                return Err(SourceError::ForbiddenHost);
            }
            Ok(())
        }
        _ => Err(SourceError::ForbiddenHost),
    }
}

fn validate_effective_url(value: &str) -> Result<(), SourceError> {
    let url = url::Url::parse(value).map_err(|_| SourceError::ForbiddenHost)?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || url.path().contains("..")
    {
        return Err(SourceError::ForbiddenHost);
    }
    match url.host_str() {
        Some("release-assets.githubusercontent.com")
        | Some("objects.githubusercontent.com")
        | Some("github-releases.githubusercontent.com") => Ok(()),
        Some("api.github.com") | Some("github.com") => {
            let mut clean = url;
            clean.set_query(None);
            clean.set_fragment(None);
            validate_initial_url(clean.as_str())
        }
        _ => Err(SourceError::ForbiddenHost),
    }
}

fn package_urls(package: &PackageRef) -> Vec<&str> {
    let mut urls = vec![package.url.as_str()];
    if let Some(url) = package.sums_url.as_deref() {
        urls.push(url);
    }
    if let Some(url) = package.file_signature_url.as_deref() {
        urls.push(url);
    }
    if let Some(url) = package.sums_signature_url.as_deref() {
        urls.push(url);
    }
    urls
}

fn download_text(
    source: &dyn ReleaseSource,
    url: Option<&str>,
    limit: u64,
    required: bool,
) -> Result<Option<String>, String> {
    let Some(url) = url else {
        return if required {
            Err(format!("发布里没有校验文件，{CANCELLED}。"))
        } else {
            Ok(None)
        };
    };
    match source.get(url, limit) {
        Ok(bytes) => String::from_utf8(bytes)
            .map(Some)
            .map_err(|_| "校验文件无法读取，已取消安装。".into()),
        Err(_) if !required => Ok(None),
        Err(SourceError::ForbiddenHost) => Err(host_rejected()),
        Err(SourceError::TooLarge) => Err(format!("校验文件超出大小限制，{CANCELLED}。")),
        Err(SourceError::Network) => Err(download_failed()),
    }
}

fn download_failed() -> String {
    format!("没有完成下载或校验，{CANCELLED}。")
}

fn host_rejected() -> String {
    format!("下载地址不是 StarDreamTech/starvault 的正式版，{CANCELLED}。")
}

fn signature_rejected() -> String {
    format!("发布签名校验失败，{CANCELLED}。")
}

fn archive_rejected() -> String {
    "安装包结构无法识别，已取消安装。请前往 Releases 手动下载。".into()
}

fn curl_program() -> Option<PathBuf> {
    let candidates = if cfg!(target_os = "windows") {
        vec![PathBuf::from(r"C:\Windows\System32\curl.exe")]
    } else {
        vec![
            PathBuf::from("/usr/bin/curl"),
            PathBuf::from("/usr/local/bin/curl"),
        ]
    };
    candidates.into_iter().find(|path| path.is_file())
}

fn curl_args(url: &str, output: &Path, limit: u64) -> Vec<String> {
    let timeout = if limit > SUMS_LIMIT { "180" } else { "20" };
    let mut args = vec![
        "--proto".into(),
        "=https".into(),
        "--proto-redir".into(),
        "=https".into(),
        "--tlsv1.2".into(),
        "--silent".into(),
        "--show-error".into(),
        "--fail".into(),
        "--location".into(),
        "--max-redirs".into(),
        "5".into(),
        "--connect-timeout".into(),
        "10".into(),
        "--max-time".into(),
        timeout.into(),
        "--max-filesize".into(),
        limit.to_string(),
        "--user-agent".into(),
        USER_AGENT.into(),
        "-o".into(),
        output.display().to_string(),
        "-w".into(),
        "%{url_effective}".into(),
    ];
    if url.starts_with("https://api.github.com/") {
        args.push("--header".into());
        args.push("Accept: application/vnd.github+json".into());
    }
    args.push("--".into());
    args.push(url.into());
    args
}

fn temp_download_path() -> PathBuf {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    std::env::temp_dir().join(format!(
        "starvault-update-{}-{nanos}-{n}.part",
        std::process::id()
    ))
}

struct TempFile<'a>(&'a Path);

impl Drop for TempFile<'_> {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.0);
    }
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), ()> {
    let _ = fs::remove_file(path);
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|_| ())?;
    file.write_all(bytes).map_err(|_| ())?;
    Ok(())
}

fn write_script(path: &Path, body: &str) -> Result<(), ()> {
    let _ = fs::remove_file(path);
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|_| ())?;
    if path.extension().is_some_and(|ext| ext == "ps1") {
        file.write_all(&[0xEF, 0xBB, 0xBF]).map_err(|_| ())?;
    }
    file.write_all(body.as_bytes()).map_err(|_| ())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o700));
    }
    Ok(())
}

fn windows_powershell() -> PathBuf {
    let system = PathBuf::from(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe");
    if system.is_file() {
        system
    } else {
        PathBuf::from("powershell.exe")
    }
}

fn spawn_detached(command: &mut Command) -> Result<(), String> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
    }
    command
        .spawn()
        .map(|_| ())
        .map_err(|_| "无法启动替换程序，已取消安装。".to_string())
}

fn zip_names(data: &[u8]) -> Result<Vec<String>, ()> {
    if data.len() < 22 || data.len() as u64 > PACKAGE_LIMIT {
        return Err(());
    }
    let eocd = find_eocd(data).ok_or(())?;
    let entries = u16::from_le_bytes([data[eocd + 10], data[eocd + 11]]);
    let cd_size =
        u32::from_le_bytes(data[eocd + 12..eocd + 16].try_into().map_err(|_| ())?) as usize;
    let cd_offset =
        u32::from_le_bytes(data[eocd + 16..eocd + 20].try_into().map_err(|_| ())?) as usize;
    if entries == u16::MAX || cd_size == u32::MAX as usize || cd_offset == u32::MAX as usize {
        return Err(());
    }
    if entries > 10_000
        || cd_size > 8 * 1024 * 1024
        || cd_offset.saturating_add(cd_size) > data.len()
    {
        return Err(());
    }
    let cd = &data[cd_offset..cd_offset + cd_size];
    let mut names = Vec::new();
    let mut pos = 0;
    while pos + 46 <= cd.len() {
        if u32::from_le_bytes(cd[pos..pos + 4].try_into().map_err(|_| ())?) != 0x0201_4b50 {
            return Err(());
        }
        let name_len =
            u16::from_le_bytes(cd[pos + 28..pos + 30].try_into().map_err(|_| ())?) as usize;
        let extra_len =
            u16::from_le_bytes(cd[pos + 30..pos + 32].try_into().map_err(|_| ())?) as usize;
        let comment_len =
            u16::from_le_bytes(cd[pos + 32..pos + 34].try_into().map_err(|_| ())?) as usize;
        let name_start = pos + 46;
        let name_end = name_start + name_len;
        let next = name_end + extra_len + comment_len;
        if next > cd.len() {
            return Err(());
        }
        let name = std::str::from_utf8(&cd[name_start..name_end]).map_err(|_| ())?;
        names.push(name.to_string());
        pos = next;
    }
    if pos != cd.len() || names.len() != entries as usize {
        return Err(());
    }
    Ok(names)
}

fn find_eocd(data: &[u8]) -> Option<usize> {
    if data.len() < 22 {
        return None;
    }
    let start = data.len().saturating_sub(22 + 65_535);
    let mut index = data.len() - 22;
    loop {
        if data[index..index + 4] == [0x50, 0x4b, 0x05, 0x06] {
            let comment_len = u16::from_le_bytes([data[index + 20], data[index + 21]]) as usize;
            if index + 22 + comment_len == data.len() {
                return Some(index);
            }
        }
        if index == start {
            break;
        }
        index -= 1;
    }
    None
}

fn validate_archive(platform: Platform, names: &[String]) -> Result<(), String> {
    if names.is_empty() || names.len() > 10_000 || names.iter().any(|name| !safe_zip_name(name)) {
        return Err(archive_rejected());
    }
    let ok = match platform {
        Platform::MacosArm64 => names
            .iter()
            .any(|name| name == "StarVault.app/Contents/MacOS/chat-history-vault"),
        Platform::WindowsX64 => windows_layout(names),
        _ => false,
    };
    if ok {
        Ok(())
    } else {
        Err(archive_rejected())
    }
}

fn safe_zip_name(name: &str) -> bool {
    if name.is_empty() || name.len() > 512 || name.contains('\0') || name.contains(':') {
        return false;
    }
    if name.starts_with('/') || name.starts_with('\\') {
        return false;
    }
    let trimmed = name.trim_end_matches(['/', '\\']);
    !trimmed.is_empty()
        && trimmed
            .split(['/', '\\'])
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

fn windows_layout(names: &[String]) -> bool {
    let normal: Vec<String> = names.iter().map(|name| name.replace('\\', "/")).collect();
    if normal.iter().any(|name| name == WINDOWS_EXE) {
        return true;
    }
    let mut top = None;
    for name in &normal {
        let Some((first, _)) = name.split_once('/') else {
            return false;
        };
        match &top {
            None => top = Some(first.to_string()),
            Some(current) if current == first => {}
            Some(_) => return false,
        }
    }
    top.is_some_and(|first| {
        normal
            .iter()
            .any(|name| name == &format!("{first}/{WINDOWS_EXE}"))
    })
}

const MACOS_SCRIPT: &str = r#"#!/bin/bash
set -euo pipefail
if [ "$#" -ne 4 ]; then
  exit 2
fi
if [ -z "${STARVAULT_UPDATE_DETACHED:-}" ]; then
  export STARVAULT_UPDATE_DETACHED=1
  /usr/bin/nohup /bin/bash "$0" "$@" >/dev/null 2>&1 &
  exit 0
fi
pid="$1"
app="$2"
zip="$3"
log="$4"
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56 57 58 59 60 61 62 63 64 65 66 67 68 69 70 71 72 73 74 75 76 77 78 79 80 81 82 83 84 85 86 87 88 89 90; do
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
  sleep 0.2
done
if kill -0 "$pid" 2>/dev/null; then
  echo "旧进程仍在运行，已取消替换" >>"$log"
  exit 1
fi
sleep 0.4
work=$(mktemp -d "${TMPDIR:-/tmp}/starvault-update.XXXXXX")
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
if ! /usr/bin/ditto -x -k -- "$zip" "$work"; then
  /usr/bin/unzip -q -- "$zip" -d "$work"
fi
new="$work/StarVault.app"
if [ ! -d "$new/Contents/MacOS" ]; then
  echo "安装包中没有 StarVault.app，已取消替换" >>"$log"
  exit 1
fi
if [ -x /usr/bin/codesign ]; then
  if /usr/bin/codesign --verify --deep --strict -- "$new" 2>"$work/codesign.err"; then
    :
  elif /usr/bin/grep -q "not signed" "$work/codesign.err"; then
    echo "安装包没有代码签名，已依靠发布校验" >>"$log"
  else
    echo "代码签名无效，已取消替换" >>"$log"
    exit 1
  fi
fi
parent=$(dirname -- "$app")
backup="$parent/StarVault.app.previous"
rm -rf -- "$backup"
if ! mv -- "$app" "$backup"; then
  echo "无法移走旧版本，已取消替换" >>"$log"
  exit 1
fi
if ! mv -- "$new" "$app"; then
  mv -- "$backup" "$app" || true
  echo "无法放入新版本，已尝试恢复旧版本" >>"$log"
  exit 1
fi
/usr/bin/xattr -dr com.apple.quarantine -- "$app" 2>/dev/null || true
rm -rf -- "$backup"
rm -f -- "$zip"
/usr/bin/open -- "$app"
"#;

const WINDOWS_SCRIPT: &str = r#"param(
  [Parameter(Mandatory = $true)][int]$ProcessId,
  [Parameter(Mandatory = $true)][string]$InstallDir,
  [Parameter(Mandatory = $true)][string]$ZipPath,
  [Parameter(Mandatory = $true)][string]$ExeName,
  [Parameter(Mandatory = $true)][string]$LogPath
)
$ErrorActionPreference = 'Stop'
function Write-Log([string]$Message) {
  Add-Content -LiteralPath $LogPath -Encoding utf8 -Value $Message
}
$deadline = 0
while ($deadline -lt 90) {
  $running = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
  if (-not $running) { break }
  Start-Sleep -Milliseconds 200
  $deadline++
}
$running = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if ($running) {
  Write-Log '旧进程仍在运行，已取消替换'
  exit 1
}
Start-Sleep -Milliseconds 400
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("starvault-update-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
  Expand-Archive -LiteralPath $ZipPath -DestinationPath $work -Force
  $source = $work
  $exe = Join-Path $source $ExeName
  if (-not (Test-Path -LiteralPath $exe)) {
    $dirs = @(Get-ChildItem -LiteralPath $work -Directory)
    if ($dirs.Count -eq 1) {
      $nested = Join-Path $dirs[0].FullName $ExeName
      if (Test-Path -LiteralPath $nested) {
        $source = $dirs[0].FullName
        $exe = $nested
      }
    }
  }
  if (-not (Test-Path -LiteralPath $exe)) {
    Write-Log '安装包中没有程序文件，已取消替换'
    exit 1
  }
  $signature = Get-AuthenticodeSignature -LiteralPath $exe
  if ($signature.Status -eq 'NotSigned') {
    Write-Log '安装包没有 Authenticode 签名，已依靠发布校验'
  } elseif ($signature.Status -ne 'Valid') {
    Write-Log '代码签名无效，已取消替换'
    exit 1
  }
  if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
  }
  $copied = $false
  for ($try = 0; $try -lt 20; $try++) {
    try {
      Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
      }
      $copied = $true
      break
    } catch {
      Start-Sleep -Milliseconds 300
    }
  }
  if (-not $copied) {
    Write-Log '无法替换旧文件，已取消'
    exit 1
  }
  $target = Join-Path $InstallDir $ExeName
  Start-Process -FilePath $target -WorkingDirectory $InstallDir | Out-Null
  Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
} finally {
  if (Test-Path -LiteralPath $work) {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
  }
}
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, collections::BTreeMap, io::Cursor};

    fn version(text: &str) -> Version {
        parse_version(text).unwrap()
    }

    #[test]
    fn version_comparison_is_numeric_and_ignores_prerelease() {
        assert!(version("0.1.10") > version("0.1.9"));
        assert!(version("v0.2.0") > version("0.1.9"));
        assert!(version("V0.10.0") > version("v0.9.9"));
        assert_eq!(version("v0.1.4"), version("0.1.4"));
        assert!(version("0.1.4") < version("0.1.5"));
        assert_eq!(parse_version("0.1.4-rc.1"), None);
        assert_eq!(parse_version("v0.2.0-beta"), None);
        assert_eq!(parse_version("0.1"), None);
        assert_eq!(parse_version("0.1.4.1"), None);
        assert_eq!(parse_version(""), None);
        assert_eq!(parse_version("starvault-0.1.5"), None);
        let current = version("0.1.4");
        assert!(version("0.1.5") > current);
        assert!(version("0.1.4") <= current);
        assert!(version("0.1.3") < current);
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    fn asset(name: &str, download: &str) -> String {
        format!(
            r#"{{"name":"{name}","state":"uploaded","size":128,"browser_download_url":"{download}"}}"#
        )
    }

    fn release_body(tag: &str, assets: &[&str]) -> String {
        format!(
            r#"{{"tag_name":"{tag}","draft":false,"prerelease":false,"assets":[{}]}}"#,
            assets.join(",")
        )
    }

    fn personal_zip(tag: &str, name: &str) -> String {
        let owner = format!("{}{}", "cpython", "666");
        format!("https://github.com/{owner}/chat-history-vault/releases/download/{tag}/{name}")
    }

    #[test]
    fn release_assets_come_only_from_stardreamtech_stable_packages() {
        let current = version("0.1.4");
        let mac = "/Applications/StarVault.app/Contents/MacOS/chat-history-vault";
        let windows = r"C:\Users\me\StarVault\chat-history-vault.exe";
        let name = "StarVault-0.2.0-macos-arm64.zip";
        let body = release_body(
            "v0.2.0",
            &[
                &asset(
                    "StarVault-0.2.0-source.tar.gz",
                    &personal_zip("v0.2.0", "source.tar.gz"),
                ),
                &asset(name, &personal_zip("v0.2.0", name)),
                &asset(
                    "starvault-0.2.0-macos-arm64.zip",
                    "https://example.invalid/nope.zip",
                ),
                &asset(SUMS_NAME, &personal_zip("v0.2.0", SUMS_NAME)),
            ],
        );
        let report = interpret_release(&body, current, Platform::MacosArm64, Path::new(mac), "");
        let UpdateReport::Available(offer) = report else {
            panic!("expected an installable macOS update");
        };
        assert_eq!(offer.version, version("0.2.0"));
        assert_eq!(
            offer.page,
            "https://github.com/StarDreamTech/starvault/releases/tag/v0.2.0"
        );
        let OfferAction::Swap(package) = offer.action else {
            panic!("installed app should swap");
        };
        assert_eq!(package.name, name);
        assert_eq!(
            package.url,
            "https://github.com/StarDreamTech/starvault/releases/download/v0.2.0/StarVault-0.2.0-macos-arm64.zip"
        );
        assert!(package.url.contains("StarDreamTech/starvault"));
        assert!(!format!("{package:?}").contains("chat-history-vault/releases"));
        assert_eq!(
            package.sums_url.as_deref(),
            Some("https://github.com/StarDreamTech/starvault/releases/download/v0.2.0/SHA256SUMS")
        );

        let source = interpret_release(
            &body,
            current,
            Platform::MacosArm64,
            Path::new("/work/desktop/target/release/chat-history-vault"),
            "",
        );
        assert!(
            matches!(source, UpdateReport::Available(offer) if matches!(offer.action, OfferAction::Manual))
        );
        let linux = interpret_release(
            &body,
            current,
            Platform::Linux,
            Path::new("/usr/bin/chat-history-vault"),
            "",
        );
        match linux {
            UpdateReport::Available(offer) => {
                assert!(offer.summary.contains("Linux"));
                assert!(matches!(offer.action, OfferAction::Manual));
            }
            other => panic!("linux must be manual, got {other:?}"),
        }
        let intel = interpret_release(&body, current, Platform::MacosX64, Path::new(mac), "");
        assert!(matches!(intel, UpdateReport::Available(offer) if offer.summary.contains("Intel")));
        let missing_windows =
            interpret_release(&body, current, Platform::WindowsX64, Path::new(windows), "");
        assert!(
            matches!(missing_windows, UpdateReport::Available(offer) if offer.summary.contains("还没有 Windows"))
        );

        let windows_name = "StarVault-0.2.0-windows-x64.zip";
        let windows_body = release_body(
            "v0.2.0",
            &[
                &asset(windows_name, &personal_zip("v0.2.0", windows_name)),
                &asset(SUMS_NAME, &personal_zip("v0.2.0", SUMS_NAME)),
            ],
        );
        let installed = interpret_release(
            &windows_body,
            current,
            Platform::WindowsX64,
            Path::new(windows),
            "",
        );
        match installed {
            UpdateReport::Available(offer) => {
                let OfferAction::Swap(package) = offer.action else {
                    panic!("windows install dir should swap");
                };
                assert_eq!(package.name, windows_name);
                assert!(package
                    .url
                    .starts_with("https://github.com/StarDreamTech/starvault/releases/download/"));
            }
            other => panic!("expected windows swap, got {other:?}"),
        }
        let cargo = interpret_release(
            &windows_body,
            current,
            Platform::WindowsX64,
            Path::new(r"D:\src\desktop\target\release\chat-history-vault.exe"),
            "",
        );
        assert!(matches!(cargo, UpdateReport::Available(offer) if offer.summary.contains("源码")));
        assert!(matches!(
            interpret_release(
                &body,
                version("0.2.0"),
                Platform::MacosArm64,
                Path::new(mac),
                ""
            ),
            UpdateReport::UpToDate(_)
        ));
        assert!(matches!(
            interpret_release(
                &body,
                version("0.3.0"),
                Platform::MacosArm64,
                Path::new(mac),
                ""
            ),
            UpdateReport::UpToDate(_)
        ));
        let prerelease = body.replace(r#""prerelease":false"#, r#""prerelease":true"#);
        assert!(matches!(
            interpret_release(
                &prerelease,
                current,
                Platform::MacosArm64,
                Path::new(mac),
                ""
            ),
            UpdateReport::Unavailable
        ));
        let draft = body.replace(r#""draft":false"#, r#""draft":true"#);
        assert!(matches!(
            interpret_release(&draft, current, Platform::MacosArm64, Path::new(mac), ""),
            UpdateReport::Unavailable
        ));
        assert!(matches!(
            interpret_release("{}", current, Platform::MacosArm64, Path::new(mac), ""),
            UpdateReport::Unavailable
        ));
        let huge = release_body(
            "v0.2.0",
            &[&format!(
                r#"{{"name":"{name}","state":"uploaded","size":999999999,"browser_download_url":"https://example.invalid/x"}}"#
            )],
        );
        assert!(matches!(
            interpret_release(&huge, current, Platform::MacosArm64, Path::new(mac), ""),
            UpdateReport::Available(offer) if offer.summary.contains("大小限制")
        ));
    }

    #[test]
    fn verification_accepts_a_matching_checksum_or_signature_and_rejects_failures() {
        let file = b"starvault-package";
        let filename = "StarVault-0.2.0-macos-arm64.zip";
        let sums = format!("{}  {filename}\n", sha256_hex(file));
        assert_eq!(
            verify_material(VerifyMaterial {
                filename,
                file,
                sums: Some(&sums),
                file_signature: None,
                sums_signature: None,
                public_key: "",
            })
            .unwrap(),
            "sha256"
        );
        let broken = sums.replace(sha256_hex(file).get(..4).unwrap(), "0000");
        let mismatch = verify_material(VerifyMaterial {
            filename,
            file,
            sums: Some(&broken),
            file_signature: None,
            sums_signature: None,
            public_key: "",
        });
        assert!(mismatch.unwrap_err().contains(CANCELLED));
        assert!(verify_material(VerifyMaterial {
            filename,
            file,
            sums: None,
            file_signature: None,
            sums_signature: None,
            public_key: "",
        })
        .unwrap_err()
        .contains("没有签名"));

        let (public_key, signature) = sign_bytes(file);
        assert_eq!(
            verify_material(VerifyMaterial {
                filename,
                file,
                sums: None,
                file_signature: Some(&signature),
                sums_signature: None,
                public_key: &public_key,
            })
            .unwrap(),
            "minisign"
        );
        let signed_and_hashed = verify_material(VerifyMaterial {
            filename,
            file,
            sums: Some(&sums),
            file_signature: Some(&signature),
            sums_signature: None,
            public_key: &public_key,
        })
        .unwrap();
        assert_eq!(signed_and_hashed, "minisign+sha256");
        let mut bad_signature = signature.trim_end().to_string();
        let last = bad_signature.pop().unwrap();
        bad_signature.push(if last == 'A' { 'B' } else { 'A' });
        let rejected = verify_material(VerifyMaterial {
            filename,
            file,
            sums: Some(&sums),
            file_signature: Some(&bad_signature),
            sums_signature: None,
            public_key: &public_key,
        });
        assert!(rejected.unwrap_err().contains("发布签名"));
        let tampered = b"starvault-package!";
        assert!(verify_material(VerifyMaterial {
            filename,
            file: tampered,
            sums: Some(&sums),
            file_signature: Some(&signature),
            sums_signature: None,
            public_key: &public_key,
        })
        .is_err());
        let ignored = verify_material(VerifyMaterial {
            filename,
            file,
            sums: Some(&sums),
            file_signature: Some(&signature),
            sums_signature: None,
            public_key: "",
        })
        .unwrap();
        assert_eq!(ignored, "sha256");

        let (sums_key, sums_signature) = sign_bytes(sums.as_bytes());
        assert_eq!(
            verify_material(VerifyMaterial {
                filename,
                file,
                sums: Some(&sums),
                file_signature: None,
                sums_signature: Some(&sums_signature),
                public_key: &sums_key,
            })
            .unwrap(),
            "minisign+sha256"
        );
        assert!(verify_material(VerifyMaterial {
            filename,
            file: tampered,
            sums: Some(&sums),
            file_signature: None,
            sums_signature: Some(&sums_signature),
            public_key: &sums_key,
        })
        .unwrap_err()
        .contains("SHA256"));

        let mac_zip = stored_zip(&[("StarVault.app/Contents/MacOS/chat-history-vault", file)]);
        assert!(validate_archive(Platform::MacosArm64, &zip_names(&mac_zip).unwrap()).is_ok());
        let slipped = stored_zip(&[("../StarVault.app/Contents/MacOS/chat-history-vault", file)]);
        assert!(validate_archive(Platform::MacosArm64, &zip_names(&slipped).unwrap()).is_err());
        let windows_zip = stored_zip(&[("chat-history-vault.exe", file), ("README.txt", b"hi")]);
        assert!(validate_archive(Platform::WindowsX64, &zip_names(&windows_zip).unwrap()).is_ok());
        assert!(validate_archive(Platform::Linux, &zip_names(&mac_zip).unwrap()).is_err());
    }

    #[test]
    fn network_errors_stay_quiet_and_never_install() {
        let current = version("0.1.4");
        let exe = Path::new("/Applications/StarVault.app/Contents/MacOS/chat-history-vault");
        let mut offline = MapSource::default();
        offline.fail(LATEST_RELEASE_URL, SourceError::Network);
        let report = check_latest(&offline, current, Platform::MacosArm64, exe);
        assert_eq!(report, UpdateReport::Unavailable);
        assert!(matches!(present(false, report.clone()), UiDecision::Silent));
        match present(true, report) {
            UiDecision::Note(text) => {
                assert!(text.contains("暂时无法检查更新"));
                assert!(text.contains("不受影响"));
                assert!(!text.contains(CANCELLED));
            }
            other => panic!("manual network failure should be a quiet note, got {other:?}"),
        }
        assert_eq!(offline.hits.borrow().as_slice(), [LATEST_RELEASE_URL]);

        let mut denied = MapSource::default();
        denied.fail(LATEST_RELEASE_URL, SourceError::ForbiddenHost);
        assert!(matches!(
            present(
                false,
                check_latest(&denied, current, Platform::MacosArm64, exe)
            ),
            UiDecision::Silent
        ));
        let mut huge = MapSource::default();
        huge.insert(LATEST_RELEASE_URL, vec![b'x'; API_LIMIT as usize + 1]);
        assert_eq!(
            check_latest(&huge, current, Platform::MacosArm64, exe),
            UpdateReport::Unavailable
        );
        let mut garbage = MapSource::default();
        garbage.insert(LATEST_RELEASE_URL, b"<html>no release</html>".to_vec());
        assert_eq!(
            check_latest(&garbage, current, Platform::MacosArm64, exe),
            UpdateReport::Unavailable
        );

        let personal = format!(
            "https://github.com/{}{}/chat-history-vault/releases/latest",
            "cpython", "666"
        );
        assert_eq!(
            CurlSource.get(&personal, 64),
            Err(SourceError::ForbiddenHost)
        );
        assert_eq!(
            validate_initial_url(
                "https://api.github.com/repos/StarDreamTech/starvault/releases/latest"
            ),
            Ok(())
        );
        assert!(validate_initial_url(
            "http://api.github.com/repos/StarDreamTech/starvault/releases/latest"
        )
        .is_err());
        assert!(validate_initial_url(
            "https://user:token@github.com/StarDreamTech/starvault/releases/download/v0.2.0/a.zip"
        )
        .is_err());
        assert!(validate_effective_url(
            "https://release-assets.githubusercontent.com/github-production-release-asset/1"
        )
        .is_ok());
        assert!(validate_effective_url("https://example.invalid/package.zip").is_err());

        let name = "StarVault-0.2.0-macos-arm64.zip";
        let package_url = official_download_url("v0.2.0", name).unwrap();
        let sums_url = official_download_url("v0.2.0", SUMS_NAME).unwrap();
        let package = PackageRef {
            name: name.into(),
            url: package_url.clone(),
            sums_url: Some(sums_url.clone()),
            file_signature_url: None,
            sums_signature_url: None,
        };
        let mut sums_down = MapSource::default();
        sums_down.fail(&sums_url, SourceError::Network);
        let dir = temp_case("network");
        let error =
            prepare_swap(&sums_down, &package, Platform::MacosArm64, exe, &dir, "").unwrap_err();
        assert!(error.contains(CANCELLED));
        assert_eq!(sums_down.hits.borrow().as_slice(), [sums_url.as_str()]);
        assert!(fs::read_dir(&dir).unwrap().next().is_none());

        let bytes = b"pkg-bytes";
        let sums = format!("{}  {name}\n", sha256_hex(bytes));
        let mut ready = MapSource::default();
        ready.insert(&sums_url, sums.into_bytes());
        ready.fail(&package_url, SourceError::Network);
        let error =
            prepare_swap(&ready, &package, Platform::MacosArm64, exe, &dir, "").unwrap_err();
        assert!(error.contains("没有完成下载或校验"));
        assert!(!dir.join(name).exists());

        let mut bad_hash = MapSource::default();
        bad_hash.insert(
            &sums_url,
            format!("{}  {name}\n", sha256_hex(b"other")).into_bytes(),
        );
        bad_hash.insert(&package_url, bytes.to_vec());
        let error =
            prepare_swap(&bad_hash, &package, Platform::MacosArm64, exe, &dir, "").unwrap_err();
        assert!(error.contains("SHA256"));
        assert!(!dir.join(name).exists());

        let archive = stored_zip(&[("StarVault.app/Contents/MacOS/chat-history-vault", bytes)]);
        let good_sums = format!("{}  {name}\n", sha256_hex(&archive));
        let mut good = MapSource::default();
        good.insert(&sums_url, good_sums.into_bytes());
        good.insert(&package_url, archive);
        let prepared = prepare_swap(&good, &package, Platform::MacosArm64, exe, &dir, "").unwrap();
        assert_eq!(prepared.kind, SwapKind::MacosApp);
        assert_eq!(
            prepared.target,
            PathBuf::from("/Applications/StarVault.app")
        );
        let script = fs::read_to_string(&prepared.script).unwrap();
        assert!(script.contains("kill -0"));
        assert!(script.contains("/usr/bin/ditto"));
        assert!(script.contains("/usr/bin/open"));
        assert!(!script.contains("eval"));
        assert!(!script.contains("github.com"));
        let args = macos_args(42, &prepared);
        assert_eq!(args[1], "42");
        assert_eq!(args[2], "/Applications/StarVault.app");
        assert!(args.iter().all(|arg| !arg.contains("github.com")));
        fs::remove_dir_all(dir).unwrap();

        let args = curl_args(LATEST_RELEASE_URL, Path::new("/tmp/body"), 32);
        let joined = args.join("\n");
        assert!(joined.contains("--proto\n=https"));
        assert!(joined.contains(USER_AGENT));
        assert!(!joined.to_ascii_lowercase().contains("authorization"));
        assert!(!joined.contains("token"));
        assert!(args.last() == Some(&LATEST_RELEASE_URL.to_string()));
    }

    #[test]
    fn swap_scripts_wait_for_the_old_process_before_replacing() {
        assert!(MACOS_SCRIPT.contains("kill -0"));
        assert!(MACOS_SCRIPT.contains("STARVAULT_UPDATE_DETACHED"));
        assert!(MACOS_SCRIPT.contains("codesign --verify"));
        assert!(MACOS_SCRIPT.contains("not signed"));
        assert!(WINDOWS_SCRIPT.contains("Get-Process"));
        assert!(WINDOWS_SCRIPT.contains("Start-Process"));
        assert!(WINDOWS_SCRIPT.contains("Get-AuthenticodeSignature"));
        assert!(WINDOWS_SCRIPT.contains("NotSigned"));
        assert!(
            matches!(installed_target(Platform::Linux, Path::new("/usr/local/bin/starvault")), Err(text) if text.contains("Linux"))
        );
        let prepared = PreparedSwap {
            kind: SwapKind::WindowsDir,
            script: PathBuf::from(r"C:\Users\me\ChatHistoryVault\updates\replace.ps1"),
            log: PathBuf::from(r"C:\Users\me\ChatHistoryVault\updates\replace.log"),
            zip: PathBuf::from(
                r"C:\Users\me\ChatHistoryVault\updates\StarVault-0.2.0-windows-x64.zip",
            ),
            target: PathBuf::from(r"C:\Users\me\StarVault"),
        };
        let args = windows_args(7, &prepared);
        assert!(args
            .windows(2)
            .any(|pair| pair == ["-ExeName", WINDOWS_EXE]));
        assert!(args.windows(2).any(|pair| pair == ["-ProcessId", "7"]));
        assert!(args.iter().any(|arg| arg == r"C:\Users\me\StarVault"));
    }

    #[test]
    fn release_checksum_workflow_does_not_embed_credentials() {
        let text = include_str!("../../.github/workflows/release-checksums.yml");
        assert!(text.contains("StarDreamTech/starvault"));
        assert!(text.contains("SHA256SUMS"));
        assert!(text.contains("packages are unchanged"));
        assert!(!text.contains("ghp_"));
        assert!(!text.contains("github_pat_"));
        let owner = format!("{}{}", "cpython", "666");
        assert!(!text.contains(&owner));
    }

    fn sign_bytes(bytes: &[u8]) -> (String, String) {
        let minisign::KeyPair { pk, sk } =
            minisign::KeyPair::generate_unencrypted_keypair().unwrap();
        let signature = minisign::sign(None, &sk, Cursor::new(bytes), None, None).unwrap();
        (pk.to_base64(), signature.into_string())
    }

    fn stored_zip(files: &[(&str, &[u8])]) -> Vec<u8> {
        fn crc32(data: &[u8]) -> u32 {
            let mut crc = 0xffff_ffffu32;
            for byte in data {
                crc ^= u32::from(*byte);
                for _ in 0..8 {
                    let mask = (crc & 1).wrapping_neg();
                    crc = (crc >> 1) ^ (0xedb8_8320 & mask);
                }
            }
            !crc
        }
        assert_eq!(crc32(b"123456789"), 0xcbf4_3926);
        let mut local = Vec::new();
        let mut central = Vec::new();
        for (name, data) in files {
            let name_bytes = name.as_bytes();
            let crc = crc32(data);
            let offset = local.len() as u32;
            local.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
            local.extend_from_slice(&20u16.to_le_bytes());
            local.extend_from_slice(&0u16.to_le_bytes());
            local.extend_from_slice(&0u16.to_le_bytes());
            local.extend_from_slice(&[0, 0, 0, 0]);
            local.extend_from_slice(&crc.to_le_bytes());
            local.extend_from_slice(&(data.len() as u32).to_le_bytes());
            local.extend_from_slice(&(data.len() as u32).to_le_bytes());
            local.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
            local.extend_from_slice(&0u16.to_le_bytes());
            local.extend_from_slice(name_bytes);
            local.extend_from_slice(data);
            central.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
            central.extend_from_slice(&20u16.to_le_bytes());
            central.extend_from_slice(&20u16.to_le_bytes());
            central.extend_from_slice(&0u16.to_le_bytes());
            central.extend_from_slice(&0u16.to_le_bytes());
            central.extend_from_slice(&[0, 0, 0, 0]);
            central.extend_from_slice(&crc.to_le_bytes());
            central.extend_from_slice(&(data.len() as u32).to_le_bytes());
            central.extend_from_slice(&(data.len() as u32).to_le_bytes());
            central.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
            central.extend_from_slice(&0u16.to_le_bytes());
            central.extend_from_slice(&0u16.to_le_bytes());
            central.extend_from_slice(&0u16.to_le_bytes());
            central.extend_from_slice(&0u16.to_le_bytes());
            central.extend_from_slice(&0u32.to_le_bytes());
            central.extend_from_slice(&offset.to_le_bytes());
            central.extend_from_slice(name_bytes);
        }
        let central_offset = local.len() as u32;
        let central_size = central.len() as u32;
        local.extend_from_slice(&central);
        local.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local.extend_from_slice(&(files.len() as u16).to_le_bytes());
        local.extend_from_slice(&(files.len() as u16).to_le_bytes());
        local.extend_from_slice(&central_size.to_le_bytes());
        local.extend_from_slice(&central_offset.to_le_bytes());
        local.extend_from_slice(&0u16.to_le_bytes());
        local
    }

    fn temp_case(name: &str) -> PathBuf {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "starvault-{name}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }

    #[derive(Default)]
    struct MapSource {
        files: BTreeMap<String, Result<Vec<u8>, SourceError>>,
        hits: RefCell<Vec<String>>,
    }

    impl MapSource {
        fn insert(&mut self, url: &str, bytes: Vec<u8>) {
            self.files.insert(url.to_string(), Ok(bytes));
        }
        fn fail(&mut self, url: &str, error: SourceError) {
            self.files.insert(url.to_string(), Err(error));
        }
    }

    impl ReleaseSource for MapSource {
        fn get(&self, url: &str, limit: u64) -> Result<Vec<u8>, SourceError> {
            self.hits.borrow_mut().push(url.to_string());
            match self.files.get(url) {
                Some(Ok(bytes)) if bytes.len() as u64 > limit => Err(SourceError::TooLarge),
                Some(result) => result.clone(),
                None => Err(SourceError::Network),
            }
        }
    }
}
