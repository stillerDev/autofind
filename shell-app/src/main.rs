#![windows_subsystem = "windows"] // GUI subsystem: no conhost spawn on ShellExecute (see below)

//! Minimal native WebView2 host for the existing AutoFind web UI.
//!
//! The window loads http://127.0.0.1:<port>/ — the exact `web/index.html`,
//! `web/style.css` and `web/app.js` served by the Python backend. No visual
//! reimplementation: the menu is byte-for-byte the web UI. Python still owns
//! the index, search and every write path.
//!
//! Window first, WebView second: the top-level window is built before the
//! backend check, so GUI -> visible window costs a few tens of milliseconds
//! even on a cold start. The Python backend is spawned from a background
//! thread and the WebView2 controller is created the moment the port
//! answers — WebView2 initialization overlaps server startup instead of
//! queueing behind it (Microsoft's own guidance: never let a simple window
//! wait on WebView2).
//!
//! `windows_subsystem = "windows"` is load-bearing for the startup benchmark:
//! a console-subsystem exe launched through ShellExecute makes Windows spin up
//! conhost first, which cost ~900 ms here. GUI subsystem removes it; the window
//! appears ~100 ms after launch, which is at or below the kensaku Rust GUI.

use std::env;
use std::fs;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use global_hotkey::{
    hotkey::{Code, HotKey, Modifiers},
    GlobalHotKeyEvent, GlobalHotKeyManager, HotKeyState,
};
use muda::{Menu, MenuItem};
#[cfg(target_os = "windows")]
use tao::platform::windows::WindowExtWindows;
use tao::{
    dpi::{LogicalSize, PhysicalPosition},
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder},
    window::WindowBuilder,
};
use tray_icon::{
    menu::MenuEvent, TrayIconBuilder, TrayIconEvent, MouseButton, MouseButtonState,
};
use wry::WebViewBuilder;

// tao removes the caption when computing the client rect but leaves WS_CAPTION
// in the real window style, so Windows still draws a title bar. Strip it after
// creation and tell the window manager to recompute the frame.
#[cfg(target_os = "windows")]
mod native {
    use std::ffi::c_void;

    pub type Hwnd = *mut c_void;

    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    pub struct Rect {
        pub left: i32,
        pub top: i32,
        pub right: i32,
        pub bottom: i32,
    }

    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    pub struct Point {
        pub x: i32,
        pub y: i32,
    }

    #[link(name = "user32")]
    extern "system" {
        pub fn GetCursorPos(lp_point: *mut Point) -> i32;
        pub fn GetWindowLongW(hwnd: Hwnd, index: i32) -> i32;
        pub fn SetWindowLongW(hwnd: Hwnd, index: i32, value: i32) -> i32;
        pub fn SetWindowPos(hwnd: Hwnd, after: Hwnd, x: i32, y: i32, cx: i32, cy: i32,
                            flags: u32) -> i32;
        pub fn GetWindowRect(hwnd: Hwnd, rect: *mut Rect) -> i32;
        pub fn FindWindowW(class: *const u16, name: *const u16) -> Hwnd;
        pub fn MoveWindow(hwnd: Hwnd, x: i32, y: i32, width: i32, height: i32,
                          repaint: i32) -> i32;
        pub fn SetLayeredWindowAttributes(hwnd: Hwnd, key: u32, alpha: u8, flags: u32) -> i32;
        pub fn IsZoomed(hwnd: Hwnd) -> i32;
        pub fn ShowWindow(hwnd: Hwnd, command: i32) -> i32;
    }

    pub const GWL_EXSTYLE: i32 = -20;
    pub const WS_EX_LAYERED: i32 = 0x0008_0000;
    pub const LWA_ALPHA: u32 = 2;
    // SWP_NOZORDER | SWP_NOACTIVATE
    pub const SWP_MOVE: u32 = 0x0004 | 0x0010;
    pub const SW_MAXIMIZE: i32 = 3;
    pub const SW_SHOW: i32 = 5;
    pub const SW_HIDE: i32 = 0;

    pub fn taskbar_rect() -> Option<(i32, i32, i32, i32)> {
        let class: Vec<u16> = "Shell_TrayWnd".encode_utf16().chain(std::iter::once(0)).collect();
        let hwnd = unsafe { FindWindowW(class.as_ptr(), std::ptr::null()) };
        if hwnd.is_null() { return None; }
        window_rect(hwnd)
    }

    pub fn window_rect(hwnd: Hwnd) -> Option<(i32, i32, i32, i32)> {
        let mut rect = Rect::default();
        if unsafe { GetWindowRect(hwnd, &mut rect) } == 0 {
            return None;
        }
        Some((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
    }

    /// Physical, virtual-screen coordinates. GetCursorPos reports the same
    /// space as tao's MonitorHandle::position/size, so the two compare directly
    /// on a mixed-DPI multi-monitor desktop (no DPI conversion guesswork).
    pub fn cursor_pos() -> (i32, i32) {
        let mut point = Point::default();
        if unsafe { GetCursorPos(&mut point) } == 0 {
            return (0, 0);
        }
        (point.x, point.y)
    }

    pub fn enable_alpha(hwnd: Hwnd) {
        unsafe {
            let ex = GetWindowLongW(hwnd, GWL_EXSTYLE);
            if ex & WS_EX_LAYERED == 0 {
                SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED);
            }
            SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA);
        }
    }

    pub fn alpha(hwnd: Hwnd, value: u8) {
        unsafe { SetLayeredWindowAttributes(hwnd, 0, value, LWA_ALPHA) };
    }

    pub fn set_topmost(hwnd: Hwnd, enabled: bool) -> i32 {
        // tao's set_always_on_top is a no-op on this layered, caption-less
        // window. Set the extended style through the real window manager.
        // Clear the z-order state first, then set it: Windows accepts a direct
        // HWND_TOPMOST call on this layered window but can leave the extended
        // style bit unchanged until a NOTOPMOST → TOPMOST transition happens.
        unsafe {
            let bottom = -2isize as Hwnd;
            SetWindowPos(hwnd, bottom, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0020);
            if enabled {
                let top = -1isize as Hwnd;
                SetWindowPos(hwnd, top, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0020);
            }
        }
        1
    }

    pub fn hide_popup(hwnd: Hwnd) {
        // The popup is shown with ShowWindow directly, so tao's cached visible
        // flag is stale and set_visible(false) may be a no-op. Hide it natively.
        unsafe {
            ShowWindow(hwnd, SW_HIDE);
            SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA);
        }
    }

    pub fn place(hwnd: Hwnd, x: i32, y: i32, width: i32, height: i32) -> i32 {
        unsafe {
            SetWindowPos(hwnd, std::ptr::null_mut(), x, y,
                         width.max(1), height.max(1), SWP_MOVE)
        }
    }
}

enum UserEvent {
    Hide,
    Show,
    Quit,
    BackendUp,
    Drag,
    Tick,
    NotifyClose,
    NotifyRestart,
    HotkeyToggle,
    HotkeyPalette,
    Screenshot,
    ScreenshotSaved { path: String, indexed: bool },
    AlwaysOnTop,
    Tray(TrayIconEvent),
    Menu(MenuEvent),
}

#[derive(Clone, Copy, PartialEq)]
enum AnimKind {
    Collapse,
    Restore,
}

type Rect4 = (i32, i32, i32, i32);

#[derive(Clone, Copy)]
struct WindowAnimation {
    kind: AnimKind,
    started: Instant,
    duration_ms: u64,
    from: Rect4,
    to: Rect4,
    restore: Rect4,
    was_maximized: bool,
}

// Both curves come from the animation craft rules: OutCubic for an exit,
// InOutCubic for a return through the same visual path.
fn out_cubic(t: f32) -> f32 {
    1.0 - (1.0 - t).powi(3)
}

fn in_out_cubic(t: f32) -> f32 {
    if t < 0.5 { 4.0 * t.powi(3) } else { 1.0 - (-2.0 * t + 2.0).powi(3) / 2.0 }
}

const TRAY_GUID: u128 = 0x8f3a2d10_6c7b_4e51_9a42_1d0f7c9e5b23;
const ANIM_MS: u64 = 380;
const FRAME_MS: u64 = 16;

fn start_animation(
    kind: AnimKind,
    from: Rect4,
    to: Rect4,
    restore: Rect4,
    was_maximized: bool,
    proxy: &tao::event_loop::EventLoopProxy<UserEvent>,
) -> WindowAnimation {
    // Ticks are sent from a worker thread; every window change is applied on
    // the GUI thread. No sleep or animation math blocks the message loop.
    let tick_proxy = proxy.clone();
    thread::spawn(move || {
        for _ in 0..=((ANIM_MS / FRAME_MS) + 1) {
            thread::sleep(Duration::from_millis(FRAME_MS));
            if tick_proxy.send_event(UserEvent::Tick).is_err() {
                break;
            }
        }
    });
    WindowAnimation {
        kind, started: Instant::now(), duration_ms: ANIM_MS,
        from, to, restore, was_maximized,
    }
}

/// Find the display under the cursor. The caller passes the monitors from the
/// event loop target (or from the loop at startup), so this works on Windows,
/// macOS and Linux as long as `native::cursor_pos` reports physical pixels.
fn monitor_at_cursor<I>(monitors: I) -> Option<tao::monitor::MonitorHandle>
where
    I: IntoIterator<Item = tao::monitor::MonitorHandle>,
{
    let (x, y) = native::cursor_pos();
    monitors.into_iter().find(|m| {
        let p = m.position();
        let s = m.size();
        x >= p.x && x < p.x.saturating_add(s.width as i32)
            && y >= p.y && y < p.y.saturating_add(s.height as i32)
    })
}

fn monitor_geometry(monitor: Option<tao::monitor::MonitorHandle>) -> (i32, i32, i32, i32) {
    monitor
        .map(|m| {
            let p = m.position();
            let s = m.size();
            (p.x, p.y, s.width as i32, s.height as i32)
        })
        .unwrap_or((0, 0, 1920, 1080))
}

/// True when any part of `rect` is visible on the current desktop. Used to
/// decide whether a saved window rect is still valid after a monitor change.
fn rect_is_onscreen<I>(monitors: I, rect: Rect4) -> bool
where
    I: IntoIterator<Item = tao::monitor::MonitorHandle>,
{
    monitors.into_iter().any(|m| {
        let (mx, my, mw, mh) = monitor_geometry(Some(m));
        rect.0 < mx.saturating_add(mw)
            && rect.0.saturating_add(rect.2) > mx
            && rect.1 < my.saturating_add(mh)
            && rect.1.saturating_add(rect.3) > my
    })
}

fn palette_rect(monitor: Option<tao::monitor::MonitorHandle>) -> Rect4 {
    let (mx, my, mw, mh) = monitor_geometry(monitor);
    let (w, h) = (640, 132);
    // Centre inside the monitor under the cursor, not inside the virtual
    // desktop: the old code ignored MonitorHandle::position and pinned a
    // second monitor's palette to (0, 0).
    ((mx + (mw - w) / 2).max(mx + 8),
     (my + mh / 3 - h / 2).max(my + 8), w, h)
}

fn notify_position(
    icon: Option<tray_icon::Rect>,
    monitor: Option<tao::monitor::MonitorHandle>,
) -> (i32, i32) {
    // Card size matches build_notify's LogicalSize.
    let (width, height) = (340, 132);
    let (mx, my, mw, mh) = monitor_geometry(monitor);

    // Anchor to the real taskbar geometry when that taskbar belongs to the
    // monitor under the cursor; otherwise use that monitor's bottom-right
    // corner. Placement stays bottom-right in both cases.
    #[cfg(target_os = "windows")]
    if let Some((left, top, right, bottom)) = native::taskbar_rect() {
        let cx = left.saturating_add(right) / 2;
        let cy = top.saturating_add(bottom) / 2;
        if cx >= mx && cx < mx.saturating_add(mw) && cy >= my && cy < my.saturating_add(mh) {
            if bottom - top < right - left {
                // Horizontal taskbar: above it (or below, if it sits on top).
                let y = if top >= my + mh / 2 { top - height - 12 } else { bottom + 12 };
                let x = (right - width - 12).min(mx + mw - width - 12);
                return (x.max(mx + 8), y.max(my + 8));
            }
            // Vertical taskbar: to its left or right, bottom-aligned.
            let x = if left <= mx + mw / 2 { right + 12 } else { left - width - 12 };
            return (x.max(mx + 8), (bottom - height - 12).max(my + 8));
        }
    }

    // Fallback to the icon rect itself when the taskbar window is unavailable.
    if let Some(icon) = icon {
        let x = (icon.position.x as i32 - width + icon.size.width as i32)
            .min(mx + mw - width - 12);
        let y = (icon.position.y as i32 - height - 10).min(my + mh - height - 12);
        return (x.max(mx + 8), y.max(my + 8));
    }
    (mx + mw - width - 16, my + mh - height - 16)
}

fn build_notify(
    target: &tao::event_loop::EventLoopWindowTarget<UserEvent>,
    proxy: &tao::event_loop::EventLoopProxy<UserEvent>,
    position: (i32, i32),
) -> Option<(tao::window::Window, wry::WebView)> {
    let window = WindowBuilder::new()
        .with_title("AutoFind notification")
        .with_decorations(false)
        .with_transparent(true)
        .with_always_on_top(true)
        // Never created visible: tao would place a blank window at the screen
        // centre for a frame. It is moved first, shown afterwards.
        .with_visible(false)
        .with_position(PhysicalPosition::new(position.0, position.1))
        .with_inner_size(LogicalSize::new(340.0, 132.0))
        .build(target)
        .ok()?;
    // Fully transparent from the first frame: the window is briefly centred
    // by tao before we can move it, and that flash must never be visible.
    let handle = window.hwnd() as native::Hwnd;
    native::enable_alpha(handle);
    native::alpha(handle, 0);
    window.set_visible(false);
    let close_proxy = proxy.clone();
    let open_proxy = proxy.clone();
    let html = NOTIFY_HTML.replace(
        "{ICON_DATA}",
        &format!("data:image/png;base64,{}", base64(ICON_PNG)),
    );
    let web = WebViewBuilder::new()
        .with_html(html)
        .with_transparent(true)
        .with_ipc_handler(move |request: wry::http::Request<String>| {
            match request.body().as_str() {
                "close" => { let _ = close_proxy.send_event(UserEvent::NotifyClose); }
                "open" => { let _ = open_proxy.send_event(UserEvent::Show); }
                _ => {}
            }
        })
        .build(&window)
        .ok()?;
    Some((window, web))
}

#[cfg(target_os = "windows")]
fn collapse_animation(
    hwnd: *mut std::ffi::c_void,
    tray_rect: Option<tray_icon::Rect>,
    saved: &mut Rect4,
    keep_saved: bool,
    proxy: &tao::event_loop::EventLoopProxy<UserEvent>,
) -> Option<WindowAnimation> {
    let from = native::window_rect(hwnd)?;
    // Palette mode is a temporary 640x132 view of the same window. Never let
    // its rect replace the remembered full-window geometry.
    if !keep_saved {
        *saved = from;
    }
    let to = tray_rect.map(|r| {
        let (x, y) = (r.position.x as i32, r.position.y as i32);
        let (w, h) = (r.size.width.max(16) as i32, r.size.height.max(16) as i32);
        (x, y, w, h)
    }).unwrap_or_else(|| {
        // No tray geometry: shrink toward the window's own centre instead of
        // guessing a screen corner. Still a real, visible transition.
        ((from.0 + from.2 / 2 - 12), (from.1 + from.3 / 2 - 12), 24, 24)
    });
    let was_maximized = unsafe { native::IsZoomed(hwnd) != 0 };
    native::enable_alpha(hwnd);
    Some(start_animation(AnimKind::Collapse, from, to, from, was_maximized, proxy))
}

#[cfg(target_os = "windows")]
fn restore_animation(
    hwnd: *mut std::ffi::c_void,
    saved: Rect4,
    tray_rect: Option<tray_icon::Rect>,
    proxy: &tao::event_loop::EventLoopProxy<UserEvent>,
) -> WindowAnimation {
    let from = tray_rect.map(|r| {
        let (x, y) = (r.position.x as i32, r.position.y as i32);
        (x, y, r.size.width.max(16) as i32, r.size.height.max(16) as i32)
    }).unwrap_or((saved.0 + saved.2 / 2 - 12, saved.1 + saved.3 / 2 - 12, 24, 24));
    native::enable_alpha(hwnd);
    native::place(hwnd, from.0, from.1, from.2, from.3);
    native::alpha(hwnd, 0);
    start_animation(AnimKind::Restore, from, saved, saved, false, proxy)
}

#[cfg(target_os = "windows")]
fn animation_frame(hwnd: *mut std::ffi::c_void, anim: &WindowAnimation) -> bool {
    let elapsed = anim.started.elapsed().as_millis() as f32;
    let t = (elapsed / anim.duration_ms as f32).min(1.0);
    let eased = if anim.kind == AnimKind::Collapse { out_cubic(t) } else { in_out_cubic(t) };
    let mix = |a: i32, b: i32| -> i32 { (a as f32 + (b as f32 - a as f32) * eased).round() as i32 };
    native::place(hwnd,
        mix(anim.from.0, anim.to.0),
        mix(anim.from.1, anim.to.1),
        mix(anim.from.2, anim.to.2),
        mix(anim.from.3, anim.to.3));
    let alpha = if anim.kind == AnimKind::Collapse {
        255 - (255.0 * eased).round() as u8
    } else {
        (255.0 * eased).round() as u8
    };
    native::alpha(hwnd, alpha);
    t >= 1.0
}

/// A balloon over the real tray icon, addressed by the same GUID the tray
/// icon was registered with. No second icon is created.
#[cfg(target_os = "windows")]
fn tray_notification(hwnd: *mut std::ffi::c_void, title: &str, body: &str) {
    use windows_sys::Win32::UI::Shell::{
        Shell_NotifyIconW, NIF_GUID, NIF_INFO, NIIF_INFO, NIM_MODIFY, NOTIFYICONDATAW,
    };
    let wide = |text: &str, out: &mut [u16]| {
        for (slot, unit) in out.iter_mut().zip(text.encode_utf16()) {
            *slot = unit;
        }
    };
    let mut data = NOTIFYICONDATAW::default();
    data.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
    data.hWnd = hwnd;
    data.uFlags = NIF_GUID | NIF_INFO;
    data.guidItem = windows_sys::core::GUID::from_u128(TRAY_GUID);
    data.dwInfoFlags = NIIF_INFO;
    wide(title, &mut data.szInfoTitle);
    wide(body, &mut data.szInfo);
    unsafe { Shell_NotifyIconW(NIM_MODIFY, &data) };
}

// Compact floating notification. CSS carries the motion; Rust only places it.
const NOTIFY_HTML: &str = include_str!("notify.html");
const ICON_PNG: &[u8] = include_bytes!("../../web/icon.png");

fn base64(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | ((chunk.get(1).copied().unwrap_or(0) as u32) << 8)
            | chunk.get(2).copied().unwrap_or(0) as u32;
        out.push(TABLE[((n >> 18) & 63) as usize] as char);
        out.push(TABLE[((n >> 12) & 63) as usize] as char);
        out.push(if chunk.len() > 1 { TABLE[((n >> 6) & 63) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { TABLE[(n & 63) as usize] as char } else { '=' });
    }
    out
}

// ---------------------------------------------------------------------------
// Screenshot hotkey: GDI grab -> dependency-free PNG -> ~/.autofind/screenshots
// ---------------------------------------------------------------------------

/// Escape one string for both a JavaScript literal and a JSON string value.
fn escape_js(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 8);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &byte in bytes {
        crc ^= byte as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

fn png_chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut checked = Vec::with_capacity(4 + data.len());
    checked.extend_from_slice(kind);
    checked.extend_from_slice(data);
    out.extend_from_slice(&crc32(&checked).to_be_bytes());
}

/// zlib stream made of stored (uncompressed) DEFLATE blocks. Screenshots are
/// large but correctness beats a hand-rolled compressor: every byte of the
/// filtered scanlines appears verbatim, so the PNG decodes on any reader.
fn zlib_stored(raw: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len() + raw.len() / 64 + 16);
    out.push(0x78);
    out.push(0x01); // CM=8, CINFO=0, FCHECK chosen so 0x7801 % 31 == 0
    let mut pos = 0usize;
    while pos < raw.len() {
        let n = (raw.len() - pos).min(0xFFFF);
        let last = if pos + n == raw.len() { 1u8 } else { 0u8 };
        out.push(last); // BFINAL=last, BTYPE=00 (stored)
        out.extend_from_slice(&(n as u16).to_le_bytes());
        out.extend_from_slice(&(!(n as u16)).to_le_bytes());
        out.extend_from_slice(&raw[pos..pos + n]);
        pos += n;
    }
    let (mut a, mut b) = (1u32, 0u32);
    for &byte in raw {
        a = (a + byte as u32) % 65521;
        b = (b + a) % 65521;
    }
    out.extend_from_slice(&((b << 16) | a).to_be_bytes());
    out
}

fn encode_png(rgb: &[u8], width: u32, height: u32) -> Vec<u8> {
    let stride = width as usize * 3;
    let mut scan = Vec::with_capacity((stride + 1) * height as usize);
    for row in rgb.chunks_exact(stride) {
        scan.push(0u8); // filter type: None
        scan.extend_from_slice(row);
    }
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]); // 8-bit RGB, no interlace
    let mut png = Vec::with_capacity(scan.len() + 128);
    png.extend_from_slice(&[137, 80, 78, 71, 13, 10, 26, 10]);
    png_chunk(&mut png, b"IHDR", &ihdr);
    png_chunk(&mut png, b"IDAT", &zlib_stored(&scan));
    png_chunk(&mut png, b"IEND", &[]);
    png
}

#[cfg(target_os = "windows")]
fn capture_screen() -> Option<(Vec<u8>, u32, u32)> {
    use windows_sys::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject,
        GetDC, GetDIBits, ReleaseDC, SelectObject, BI_RGB, BITMAPINFO, BITMAPINFOHEADER,
        DIB_RGB_COLORS, SRCCOPY,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetSystemMetrics, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN,
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
    };
    // Virtual-screen metrics cover every monitor; no hardcoded coordinates.
    let vx = unsafe { GetSystemMetrics(SM_XVIRTUALSCREEN) };
    let vy = unsafe { GetSystemMetrics(SM_YVIRTUALSCREEN) };
    let vw = unsafe { GetSystemMetrics(SM_CXVIRTUALSCREEN) };
    let vh = unsafe { GetSystemMetrics(SM_CYVIRTUALSCREEN) };
    if vw <= 0 || vh <= 0 {
        return None;
    }
    unsafe {
        let screen = GetDC(std::ptr::null_mut()); // NULL = entire virtual screen
        if screen.is_null() {
            return None;
        }
        let mem = CreateCompatibleDC(screen);
        let bitmap = CreateCompatibleBitmap(screen, vw, vh);
        let old = SelectObject(mem, bitmap);
        let blitted = BitBlt(mem, 0, 0, vw, vh, screen, vx, vy, SRCCOPY);
        let mut info: BITMAPINFO = std::mem::zeroed();
        info.bmiHeader.biSize = std::mem::size_of::<BITMAPINFOHEADER>() as u32;
        info.bmiHeader.biWidth = vw;
        info.bmiHeader.biHeight = -vh; // negative = top-down rows
        info.bmiHeader.biPlanes = 1;
        info.bmiHeader.biBitCount = 32;
        info.bmiHeader.biCompression = BI_RGB;
        let mut bgra = vec![0u8; (vw as usize) * (vh as usize) * 4];
        let read = GetDIBits(
            mem, bitmap, 0, vh as u32,
            bgra.as_mut_ptr() as *mut _, &mut info, DIB_RGB_COLORS,
        );
        SelectObject(mem, old);
        DeleteObject(bitmap);
        DeleteDC(mem);
        ReleaseDC(std::ptr::null_mut(), screen);
        if blitted == 0 || read == 0 {
            return None;
        }
        let mut rgb = Vec::with_capacity(bgra.len() / 4 * 3);
        for px in bgra.chunks_exact(4) {
            rgb.push(px[2]); // BGRA -> RGB
            rgb.push(px[1]);
            rgb.push(px[0]);
        }
        Some((rgb, vw as u32, vh as u32))
    }
}

#[cfg(not(target_os = "windows"))]
fn capture_screen() -> Option<(Vec<u8>, u32, u32)> {
    None
}

#[cfg(target_os = "windows")]
fn screenshot_stamp() -> String {
    use windows_sys::Win32::Foundation::SYSTEMTIME;
    use windows_sys::Win32::System::SystemInformation::GetLocalTime;
    let mut time = SYSTEMTIME::default();
    unsafe { GetLocalTime(&mut time) };
    format!("{:04}{:02}{:02}-{:02}{:02}{:02}-{:03}",
            time.wYear, time.wMonth, time.wDay,
            time.wHour, time.wMinute, time.wSecond, time.wMilliseconds)
}

#[cfg(not(target_os = "windows"))]
fn screenshot_stamp() -> String {
    let millis = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
    format!("{}", millis)
}

/// Minimal loopback HTTP client for the local Python server. The server sends
/// Content-Length on every reply, so `Connection: close` + read-to-end is
/// enough; no HTTP dependency is pulled into the launcher.
fn http_exchange(port: u16, method: &str, route: &str, body: Option<&str>) -> Option<String> {
    use std::io::{Read, Write};
    let address = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(900)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
    let host = format!("127.0.0.1:{port}");
    let mut request = format!(
        "{method} {route} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nAccept: */*\r\n"
    );
    if let Some(payload) = body {
        request.push_str(&format!(
            "Content-Type: application/json\r\nOrigin: http://{host}\r\nContent-Length: {}\r\n\r\n{}",
            payload.len(), payload
        ));
    } else {
        request.push_str("\r\n");
    }
    stream.write_all(request.as_bytes()).ok()?;
    let mut response = Vec::new();
    stream.read_to_end(&mut response).ok()?;
    let text = String::from_utf8_lossy(&response);
    let status = text.lines().next()?;
    if !status.contains(" 200 ") && !status.contains(" 202 ") {
        return None;
    }
    let split = text.find("\r\n\r\n")? + 4;
    Some(text[split..].to_string())
}

/// Ask the running server what it can do instead of assuming a route exists.
/// Only a POST route whose own path mentions screenshot/capture/shot counts;
/// this repo's server has none, so callers fall back to the web message.
fn discover_shot_endpoint(port: u16) -> Option<String> {
    let doc = http_exchange(port, "GET", "/api/openapi.json", None)?;
    let bytes = doc.as_bytes();
    let mut cursor = 0usize;
    while cursor < bytes.len() {
        let relative = doc[cursor..].find("\"/")?;
        let start = cursor + relative + 1; // skip the opening quote
        let mut end = start;
        while end < bytes.len()
            && (bytes[end].is_ascii_alphanumeric() || b"/-_.{}".contains(&bytes[end]))
        {
            end += 1;
        }
        if end == start {
            cursor = start + 1;
            continue;
        }
        let path = &doc[start..end];
        let lower = path.to_ascii_lowercase();
        if lower.contains("screenshot") || lower.contains("capture") || lower.contains("shot") {
            let lookahead = (end + 320).min(doc.len());
            // OpenAPI is pretty-printed: `"post": {`, not `"post":{`.
            if doc[end..lookahead].replace(' ', "").contains("\"post\":{") {
                return Some(path.to_string());
            }
        }
        cursor = end;
    }
    None
}

/// Runs off the GUI thread. Always saves the PNG; indexing is attempted only
/// through a route the server advertises (or one the operator declared via
/// AUTOFIND_SHOT_INDEX_ENDPOINT). No route is fabricated.
fn screenshot_worker(port: u16, proxy: tao::event_loop::EventLoopProxy<UserEvent>) {
    let done = |path: String, indexed: bool| {
        let _ = proxy.send_event(UserEvent::ScreenshotSaved { path, indexed });
    };
    let (rgb, width, height) = match capture_screen() {
        Some(frame) => frame,
        None => {
            done(String::new(), false);
            return;
        }
    };
    let directory = home().join("screenshots");
    if fs::create_dir_all(&directory).is_err() {
        done(String::new(), false);
        return;
    }
    let file = directory.join(format!("screenshot-{}.png", screenshot_stamp()));
    if fs::write(&file, encode_png(&rgb, width, height)).is_err() {
        done(String::new(), false);
        return;
    }
    let absolute = file.to_string_lossy().into_owned();
    let route = env::var("AUTOFIND_SHOT_INDEX_ENDPOINT")
        .ok()
        .filter(|value| value.starts_with('/'))
        .or_else(|| discover_shot_endpoint(port));
    let indexed = match route {
        Some(path) => {
            let payload = format!("{{\"path\":\"{}\"}}", escape_js(&absolute));
            http_exchange(port, "POST", &path, Some(&payload)).is_some()
        }
        None => false,
    };
    done(absolute, indexed);
}

const INIT: &str = r#"
(function () {
  function send(message) {
    try { window.ipc.postMessage(message); } catch (error) {}
  }
  window.pywebview = {
    api: {
      hide: function () { send('hide'); },
      quit: function () { send('quit'); },
      always_on_top: function () { send('always-on-top'); },
      onboarding_finish: function () {},
      onboarding_reopen: function () {}
    }
  };
  // No native title bar: the app's own header is the drag surface. Ignore
  // inputs/buttons so clicking the search field still puts the caret there.
  document.addEventListener('mousedown', function (event) {
    if (event.button !== 0) return;
    if (event.target.closest('button, a, input, textarea, select, label')) return;
    if (!event.target.closest('.hero-top, .pywebview-drag-region')) return;
    send('drag');
  }, true);
  window.dispatchEvent(new Event('pywebviewready'));
})();
"#;

fn home() -> PathBuf {
    if let Some(path) = env::var_os("AUTOFIND_HOME") {
        return PathBuf::from(path);
    }
    let base = env::var_os("USERPROFILE")
        .or_else(|| env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    base.join(".autofind")
}

fn port() -> u16 {
    if let Ok(text) = env::var("AUTOFIND_PORT") {
        if let Ok(value) = text.trim().parse::<u16>() {
            if value > 0 {
                return value;
            }
        }
    }
    fs::read_to_string(home().join("app.port"))
        .ok()
        .and_then(|text| text.trim().parse::<u16>().ok())
        .filter(|port| *port > 0)
        .unwrap_or(8765)
}

fn listening(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

/// Saved launcher state. Plain whitespace-separated text on purpose: no new
/// serialization dependency and the operator can edit the flags by hand.
///
/// `~/.autofind/window.txt`:
///   x y w h always_on_top start_minimized quiet
///
/// The first five fields are the original geometry record. The two flags are
/// optional for backwards compatibility: an older five-field file loads with
/// both flags off. `write_window_state` re-reads them before writing, so an
/// external edit to either flag survives the next collapse.
fn truthy(field: &str) -> bool {
    matches!(field.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on")
}

struct WindowState {
    rect: Rect4,
    always_on_top: bool,
    start_minimized: bool,
    quiet: bool,
}

fn read_window_state() -> WindowState {
    let text = fs::read_to_string(home().join("window.txt")).unwrap_or_default();
    let parts: Vec<&str> = text.split_whitespace().collect();
    if parts.len() >= 5 {
        if let (Ok(x), Ok(y), Ok(w), Ok(h)) = (
            parts[0].parse::<i32>(), parts[1].parse::<i32>(),
            parts[2].parse::<i32>(), parts[3].parse::<i32>(),
        ) {
            return WindowState {
                rect: (x, y, w.max(720), h.max(520)),
                always_on_top: parts[4] == "1",
                start_minimized: parts.get(5).map(|f| truthy(f)).unwrap_or(false),
                quiet: parts.get(6).map(|f| truthy(f)).unwrap_or(false),
            };
        }
    }
    WindowState {
        rect: (0, 0, 920, 720),
        always_on_top: false,
        start_minimized: false,
        quiet: false,
    }
}

fn write_window_state(rect: Rect4, always_on_top: bool) {
    // Preserve the two hand-editable flags instead of clobbering them with the
    // values captured at process start.
    let previous = read_window_state();
    let text = format!("{} {} {} {} {} {} {}",
                       rect.0, rect.1, rect.2, rect.3,
                       if always_on_top { 1 } else { 0 },
                       if previous.start_minimized { 1 } else { 0 },
                       if previous.quiet { 1 } else { 0 });
    let _ = fs::write(home().join("window.txt"), text);
}

/// Spawn the Python backend if the port is still dark.
///
/// Sources: `PYTHON` is an interpreter -> `-m core.cli serve`.
/// Frozen (PyInstaller): `AUTOFIND_FROZEN=1` and `PYTHON` is the app exe
/// itself -> `AutoFind.exe serve`, which run.py routes to the same CLI.
fn spawn_backend(port: u16) {
    let python = env::var_os("PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("python"));
    let root = env::current_dir().unwrap_or_default();
    let port_arg = port.to_string();
    if env::var_os("AUTOFIND_FROZEN").is_some() {
        let _ = Command::new(&python)
            .args(["serve", "--no-browser", "--port", &port_arg])
            .spawn();
    } else {
        let _ = Command::new(&python)
            .current_dir(root)
            .args(["-m", "core.cli", "serve", "--no-browser", "--port", &port_arg])
            .spawn();
    }
}

fn main() -> wry::Result<()> {
    let debug = env::var_os("AUTOFIND_SHELL_DEBUG").is_some();
    if debug {
        eprintln!(
            "[shell] main-enter epoch_ms={}",
            SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis()
        );
    }
    let t0 = Instant::now();
    let mark = |label: &str, t0: Instant| {
        if debug {
            eprintln!("[shell] {} {:.1} ms", label, t0.elapsed().as_secs_f64() * 1000.0);
        }
    };
    mark("start", t0);
    let port = port();
    let url = format!("http://127.0.0.1:{port}/");

    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    mark("event-loop", t0);
    let state = read_window_state();
    let scale = event_loop.primary_monitor().map(|m| m.scale_factor()).unwrap_or(1.0);
    // A saved rect from an unplugged monitor must not strand the window off
    // screen. In that case only, re-centre it on the display under the cursor.
    let mut initial_rect = state.rect;
    if !rect_is_onscreen(event_loop.available_monitors(), initial_rect) {
        if let Some(monitor) = monitor_at_cursor(event_loop.available_monitors()) {
            let (mx, my, mw, mh) = monitor_geometry(Some(monitor));
            initial_rect = (
                (mx + (mw - initial_rect.2) / 2).max(mx + 8),
                (my + (mh - initial_rect.3) / 2).max(my + 8),
                initial_rect.2.min(mw.saturating_sub(16)),
                initial_rect.3.min(mh.saturating_sub(16)),
            );
        }
    }
    let (sx, sy, sw, sh) = initial_rect;
    let window = WindowBuilder::new()
        .with_title("AutoFind")
        // The web UI already draws its own window controls; the native frame
        // only adds a second title bar on top of the app.
        .with_decorations(false)
        .with_resizable(true)
        .with_always_on_top(state.always_on_top)
        .with_inner_size(LogicalSize::new(sw as f64 / scale, sh as f64 / scale))
        .with_min_inner_size(LogicalSize::new(720.0, 560.0))
        // start_minimized is honoured at startup: built hidden, so tao never
        // paints a frame on the wrong monitor before the tray takes over.
        .with_visible(!state.start_minimized)
        .build(&event_loop)
        .expect("create window");
    #[cfg(target_os = "windows")]
    {
        native::place(window.hwnd() as native::Hwnd, sx, sy, sw, sh);
        native::set_topmost(window.hwnd() as native::Hwnd, state.always_on_top);
    }
    #[cfg(target_os = "windows")]
    {
        use tao::platform::windows::WindowExtWindows;
        const GWL_STYLE: i32 = -16;
        const WS_CAPTION: i32 = 0x00C0_0000;
        // SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        const SWP_FRAME: u32 = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020;
        let hwnd = window.hwnd() as native::Hwnd;
        unsafe {
            let style = native::GetWindowLongW(hwnd, GWL_STYLE);
            native::SetWindowLongW(hwnd, GWL_STYLE, style & !WS_CAPTION);
            native::SetWindowPos(hwnd, std::ptr::null_mut(), 0, 0, 0, 0, SWP_FRAME);
        }
    }
    mark("window-built", t0);
    let debug = debug; // plain copy: the run closure below moves it

    let proxy = event_loop.create_proxy();

    // Global hotkeys: the manager must be created on the event-loop thread.
    // Ctrl+Alt+Space = show/hide, Ctrl+Alt+P = compact quick palette,
    // Ctrl+Alt+S = capture the virtual screen into ~/.autofind/screenshots.
    let hotkeys_manager = GlobalHotKeyManager::new().ok();
    let hotkey_toggle = HotKey::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::Space);
    let hotkey_palette = HotKey::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyP);
    let hotkey_shot = HotKey::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyS);
    let hotkey_top = HotKey::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyT);
    if let Some(manager) = &hotkeys_manager {
        let _ = manager.register(hotkey_toggle);
        let _ = manager.register(hotkey_palette);
        let _ = manager.register(hotkey_shot);
        let _ = manager.register(hotkey_top);
    }
    let hotkey_proxy = proxy.clone();
    let toggle_id = hotkey_toggle.id();
    let palette_id = hotkey_palette.id();
    let shot_id = hotkey_shot.id();
    let top_id = hotkey_top.id();
    GlobalHotKeyEvent::set_event_handler(Some(move |event: global_hotkey::GlobalHotKeyEvent| {
        if event.state != HotKeyState::Pressed {
            return;
        }
        let message = if event.id == toggle_id {
            UserEvent::HotkeyToggle
        } else if event.id == palette_id {
            UserEvent::HotkeyPalette
        } else if event.id == shot_id {
            UserEvent::Screenshot
        } else if event.id == top_id {
            UserEvent::AlwaysOnTop
        } else {
            return;
        };
        let _ = hotkey_proxy.send_event(message);
    }));

    // Tray icon with a menu, like the old Python app. A left click raises the
    // window; right click opens the menu with an explicit "Выйти" so the app
    // can be closed without hunting for the tiny ✕ in the corner.
    let menu = Menu::new();
    let show_item = MenuItem::with_id("show", "Открыть приложение", true, None);
    let hide_item = MenuItem::with_id("hide", "Свернуть", true, None);
    let quit_item = MenuItem::with_id("quit", "Выход", true, None);
    for item in [&show_item as &dyn muda::IsMenuItem, &hide_item, &quit_item] {
        let _ = menu.append(item);
    }
    let tray_proxy = proxy.clone();
    TrayIconEvent::set_event_handler(Some(move |event| {
        let _ = tray_proxy.send_event(UserEvent::Tray(event));
    }));
    let menu_proxy = proxy.clone();
    MenuEvent::set_event_handler(Some(move |event| {
        let _ = menu_proxy.send_event(UserEvent::Menu(event));
    }));
    let icon = [
        PathBuf::from("web/icon.ico"),
        std::env::current_exe().unwrap_or_default()
            .parent().map(|p| p.join("..").join("..").join("web").join("icon.ico"))
            .unwrap_or_default(),
        std::env::current_exe().unwrap_or_default()
            .parent().map(|p| p.join("web").join("icon.ico"))
            .unwrap_or_default(),
    ].iter().find_map(|path| tray_icon::Icon::from_path(path, None).ok());
    // One icon only, addressed by a stable GUID so the balloon notification
    // can find it. `event_loop.run` never returns, so keeping it in the
    // closure keeps it alive for the whole process.
    let tray: Option<tray_icon::TrayIcon> = icon.and_then(|icon| {
        TrayIconBuilder::new()
            .with_id("autofind-tray")
            .with_guid(TRAY_GUID)
            .with_icon(icon)
            .with_menu(Box::new(menu))
            .with_tooltip("AutoFind")
            .build()
            .ok()
    });
    if tray.is_some() && debug {
        eprintln!("[shell] tray ready {:.1} ms", t0.elapsed().as_secs_f64() * 1000.0);
    }

    // Stay resident like the kensaku tray app: a second launch only writes a
    // flag, and the existing window reappears in tens of milliseconds.
    let _ = fs::write(home().join("shell.pid"), std::process::id().to_string());
    let flag = home().join("shell-show.flag");
    let show_proxy = proxy.clone();
    thread::spawn(move || loop {
        if flag.exists() {
            let _ = fs::remove_file(&flag);
            let _ = show_proxy.send_event(UserEvent::Show);
        }
        thread::sleep(Duration::from_millis(40));
    });

    // Backend on a worker thread: the window above is already visible while
    // the interpreter spins up. As soon as the port answers (it may already
    // be listening — the resident-server case) the loop builds the WebView.
    let backend_proxy = proxy.clone();
    thread::spawn(move || {
        if !listening(port) {
            spawn_backend(port);
            let deadline = Instant::now() + Duration::from_secs(15);
            while Instant::now() < deadline && !listening(port) {
                thread::sleep(Duration::from_millis(50));
            }
        }
        let _ = backend_proxy.send_event(UserEvent::BackendUp);
    });

    let mut _webview: Option<wry::WebView> = None;
    let _hotkeys_manager = hotkeys_manager; // registered hotkeys live for the process
    let mut palette_mode = false;
    let mut always_on_top = state.always_on_top;
    let quiet = state.quiet;
    #[cfg(target_os = "windows")]
    let hwnd = window.hwnd() as native::Hwnd;
    #[cfg(target_os = "windows")]
    let mut saved_rect: Rect4 = native::window_rect(hwnd).unwrap_or((0, 0, 920, 720));
    #[cfg(target_os = "windows")]
    let mut animation: Option<WindowAnimation> = None;
    #[cfg(not(target_os = "windows"))]
    let mut animation: Option<WindowAnimation> = None;
    // Lazily created on the first collapse: a second WebView2 controller costs
    // startup time, and most sessions never need the popup.
    let mut notify: Option<(tao::window::Window, wry::WebView)> = None;

    event_loop.run(move |event, target, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(UserEvent::BackendUp) => {
                if debug {
                    eprintln!("[shell] backend-up {:.1} ms", t0.elapsed().as_secs_f64() * 1000.0);
                }
                if _webview.is_some() {
                    return;
                }
                let ipc_proxy = proxy.clone();
                let builder = WebViewBuilder::new()
                    .with_url(&url)
                    .with_initialization_script(INIT)
                    .with_ipc_handler(move |request: wry::http::Request<String>| {
                        match request.body().as_str() {
                            "hide" => {
                                let _ = ipc_proxy.send_event(UserEvent::Hide);
                            }
                            "quit" => {
                                let _ = ipc_proxy.send_event(UserEvent::Quit);
                            }
                            "drag" => {
                                let _ = ipc_proxy.send_event(UserEvent::Drag);
                            }
                            "always-on-top" => {
                                let _ = ipc_proxy.send_event(UserEvent::AlwaysOnTop);
                            }
                            _ => {}
                        }
                    });
                #[cfg(any(target_os = "windows", target_os = "macos", target_os = "ios", target_os = "android"))]
                {
                    _webview = builder.build(&window).ok();
                }
                #[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "ios", target_os = "android")))]
                {
                    use tao::platform::unix::WindowExtUnix;
                    use wry::WebViewBuilderExtUnix;
                    _webview = window.default_vbox().and_then(|vbox| builder.build_gtk(vbox).ok());
                }
            }
            Event::UserEvent(UserEvent::Drag) => {
                let _ = window.drag_window();
            }
            Event::UserEvent(UserEvent::AlwaysOnTop) => {
                if env::var_os("AUTOFIND_SHELL_DEBUG").is_some() {
                    eprintln!("[shell] always-on-top event, current={always_on_top}");
                }
                always_on_top = !always_on_top;
                #[cfg(target_os = "windows")]
                {
                    native::set_topmost(hwnd, always_on_top);
                }
                #[cfg(not(target_os = "windows"))]
                window.set_always_on_top(always_on_top);
                write_window_state(saved_rect, always_on_top);
            }
            Event::UserEvent(UserEvent::HotkeyToggle) => {
                #[cfg(target_os = "windows")]
                if animation.is_none() && tray.is_some() {
                    if window.is_visible() {
                        let rect = tray.as_ref().and_then(|t| t.rect());
                        animation = collapse_animation(hwnd, rect, &mut saved_rect, palette_mode, &proxy);
                    } else {
                        palette_mode = false;
                        let rect = tray.as_ref().and_then(|t| t.rect());
                        animation = Some(restore_animation(hwnd, saved_rect, rect, &proxy));
                        window.set_visible(true);
                    }
                }
            }
            Event::UserEvent(UserEvent::HotkeyPalette) => {
                #[cfg(target_os = "windows")]
                if animation.is_none() && tray.is_some() {
                    // The palette belongs to the display under the cursor,
                    // not the display that last held the main window.
                    let target = palette_rect(monitor_at_cursor(target.available_monitors())
                        .or_else(|| window.current_monitor()));
                    if palette_mode && window.is_visible() {
                        palette_mode = false;
                        let from = native::window_rect(hwnd).unwrap_or(target);
                        native::enable_alpha(hwnd);
                        native::alpha(hwnd, 255);
                        animation = Some(start_animation(
                            AnimKind::Restore, from, saved_rect, saved_rect, false, &proxy,
                        ));
                    } else {
                        palette_mode = true;
                        if window.is_visible() {
                            let from = native::window_rect(hwnd).unwrap_or(target);
                            native::enable_alpha(hwnd);
                            native::alpha(hwnd, 255);
                            animation = Some(start_animation(
                                AnimKind::Restore, from, target, target, false, &proxy,
                            ));
                        } else {
                            let rect = tray.as_ref().and_then(|t| t.rect());
                            animation = Some(restore_animation(hwnd, target, rect, &proxy));
                            window.set_visible(true);
                        }
                    }
                }
            }
            Event::UserEvent(UserEvent::Screenshot) => {
                // Capture, PNG-encode and the loopback HTTP probe all block;
                // none of them may stall the event loop.
                let shot_proxy = proxy.clone();
                let shot_port = port;
                thread::spawn(move || screenshot_worker(shot_port, shot_proxy));
            }
            Event::UserEvent(UserEvent::ScreenshotSaved { path, indexed }) => {
                if let Some(web) = _webview.as_ref() {
                    let escaped = escape_js(&path);
                    let script = format!(
                        "window.__autofindLastScreenshot = {{ path: \"{escaped}\", indexed: {indexed} }}; \
                         window.dispatchEvent(new CustomEvent('autofind:screenshot', \
                         {{ detail: {{ path: \"{escaped}\", indexed: {indexed} }} }}));"
                    );
                    let _ = web.evaluate_script(&script);
                }
            }
            Event::UserEvent(UserEvent::NotifyRestart) => {
                if let Some((_, web)) = notify.as_ref() {
                    let _ = web.evaluate_script(
                        "window.__afNotifyRestart && window.__afNotifyRestart()",
                    );
                }
            }
            Event::UserEvent(UserEvent::NotifyClose) => {
                if let Some((popup, _)) = notify.as_ref() {
                    native::hide_popup(popup.hwnd() as native::Hwnd);
                }
            }
            Event::UserEvent(UserEvent::Tick) => {
                #[cfg(target_os = "windows")]
                if let Some(anim) = animation.as_ref() {
                    if animation_frame(hwnd, anim) {
                        let finished = *anim;
                        animation = None;
                        if finished.kind == AnimKind::Collapse {
                            window.set_visible(false);
                            native::alpha(hwnd, 255);
                            native::place(hwnd, finished.restore.0, finished.restore.1,
                                          finished.restore.2, finished.restore.3);
                            write_window_state(finished.restore, always_on_top);
                            // Quiet mode suppresses both the custom card and
                            // the native balloon fallback.
                            #[cfg(target_os = "windows")]
                            if !quiet {
                                let icon = tray.as_ref().and_then(|t| t.rect());
                                let monitor = monitor_at_cursor(target.available_monitors())
                                    .or_else(|| window.current_monitor());
                                let (x, y) = notify_position(icon, monitor);
                                if notify.is_none() {
                                    notify = build_notify(target, &proxy, (x, y));
                                }
                                if let Some((popup, _)) = notify.as_ref() {
                                    let hwnd_bits = popup.hwnd() as usize;
                                    let restart_proxy = proxy.clone();
                                    // Move while still hidden, then show directly:
                                    // no centred blank frame is ever painted.
                                    thread::spawn(move || {
                                        thread::sleep(Duration::from_millis(80));
                                        unsafe {
                                            native::MoveWindow(
                                                hwnd_bits as native::Hwnd, x, y, 340, 132, 1,
                                            );
                                            native::ShowWindow(
                                                hwnd_bits as native::Hwnd, native::SW_SHOW,
                                            );
                                        }
                                        // ShowWindow applies tao's cached centre
                                        // position; move the now-visible window,
                                        // still at alpha 0, then reveal it.
                                        thread::sleep(Duration::from_millis(40));
                                        unsafe {
                                            native::MoveWindow(
                                                hwnd_bits as native::Hwnd, x, y, 340, 132, 1,
                                            );
                                        }
                                        native::alpha(hwnd_bits as native::Hwnd, 255);
                                        let _ = restart_proxy.send_event(UserEvent::NotifyRestart);
                                    });
                                } else if let Some(tray) = tray.as_ref() {
                                    // Fallback: the native balloon if the card
                                    // could not be created.
                                    tray_notification(tray.window_handle(), "AutoFind",
                                        "Приложение продолжает работать в системном трее");
                                }
                            }
                            #[cfg(target_os = "windows")]
                            if quiet {
                                if let Some((popup, _)) = notify.as_ref() {
                                    native::hide_popup(popup.hwnd() as native::Hwnd);
                                }
                            }
                        } else {
                            window.set_visible(true);
                            native::alpha(hwnd, 255);
                            window.set_focus();
                            if palette_mode {
                                if let Some(web) = _webview.as_ref() {
                                    let _ = web.evaluate_script(
                                        "document.getElementById('q') && document.getElementById('q').focus()",
                                    );
                                }
                            }
                            if finished.was_maximized {
                                unsafe { native::ShowWindow(hwnd, native::SW_MAXIMIZE) };
                            }
                        }
                    }
                }
                #[cfg(not(target_os = "windows"))]
                { animation = None; }
            }
            Event::UserEvent(UserEvent::Hide) => {
                #[cfg(target_os = "windows")]
                if animation.is_none() && tray.is_some() {
                    let rect = tray.as_ref().and_then(|t| t.rect());
                    animation = collapse_animation(hwnd, rect, &mut saved_rect, palette_mode, &proxy);
                }
            }
            Event::UserEvent(UserEvent::Show) => {
                #[cfg(target_os = "windows")]
                if animation.is_none() {
                    palette_mode = false;
                    if window.is_visible() {
                        window.set_focus();
                    } else if tray.is_some() {
                        let rect = tray.as_ref().and_then(|t| t.rect());
                        animation = Some(restore_animation(hwnd, saved_rect, rect, &proxy));
                        // Alpha is already 0; showing now makes the growth visible.
                        window.set_visible(true);
                        if let Some((popup, _)) = notify.as_ref() {
                    native::hide_popup(popup.hwnd() as native::Hwnd);
                }
                    } else {
                        window.set_visible(true);
                        window.set_focus();
                    }
                }
                #[cfg(not(target_os = "windows"))]
                { window.set_visible(true); window.set_focus(); }
            }
            Event::UserEvent(UserEvent::Tray(TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Down,
                ..
            })) | Event::UserEvent(UserEvent::Tray(TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            })) => {
                #[cfg(target_os = "windows")]
                if animation.is_none() {
                    palette_mode = false;
                    if window.is_visible() {
                        window.set_focus();
                    } else {
                        let rect = tray.as_ref().and_then(|t| t.rect());
                        animation = Some(restore_animation(hwnd, saved_rect, rect, &proxy));
                        // Alpha is already 0; showing now makes the growth visible.
                        window.set_visible(true);
                        if let Some((popup, _)) = notify.as_ref() {
                    native::hide_popup(popup.hwnd() as native::Hwnd);
                }
                    }
                }
                #[cfg(not(target_os = "windows"))]
                { window.set_visible(true); window.set_focus(); }
            }
            Event::UserEvent(UserEvent::Menu(event)) => match event.id.as_ref() {
                "quit" => *control_flow = ControlFlow::Exit,
                "hide" => {
                    #[cfg(target_os = "windows")]
                    if animation.is_none() && tray.is_some() {
                        let rect = tray.as_ref().and_then(|t| t.rect());
                        animation = collapse_animation(hwnd, rect, &mut saved_rect, palette_mode, &proxy);
                    }
                }
                "show" => {
                    #[cfg(target_os = "windows")]
                    if animation.is_none() {
                        palette_mode = false;
                        if window.is_visible() {
                            window.set_focus();
                        } else if tray.is_some() {
                            let rect = tray.as_ref().and_then(|t| t.rect());
                            animation = Some(restore_animation(hwnd, saved_rect, rect, &proxy));
                        // Alpha is already 0; showing now makes the growth visible.
                        window.set_visible(true);
                        if let Some((popup, _)) = notify.as_ref() {
                    native::hide_popup(popup.hwnd() as native::Hwnd);
                }
                        } else {
                            window.set_visible(true);
                            window.set_focus();
                        }
                    }
                    #[cfg(not(target_os = "windows"))]
                    { window.set_visible(true); window.set_focus(); }
                }
                _ => {}
            },
            Event::UserEvent(UserEvent::Quit) => *control_flow = ControlFlow::Exit,
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                // X means "to the tray", never "kill the process". If no tray
                // icon exists, fall back to a real exit instead of losing the
                // window forever.
                #[cfg(target_os = "windows")]
                if animation.is_none() && tray.is_some() {
                    let rect = tray.as_ref().and_then(|t| t.rect());
                    animation = collapse_animation(hwnd, rect, &mut saved_rect, palette_mode, &proxy);
                    return;
                }
                #[cfg(not(target_os = "windows"))]
                { let _ = &tray; }
                *control_flow = ControlFlow::Exit;
            }
            _ => {}
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn png_round_trips_scanlines() {
        let (width, height) = (3usize, 2usize);
        let rgb: Vec<u8> = (0..width * height * 3).map(|i| (i * 17) as u8).collect();
        let png = encode_png(&rgb, width as u32, height as u32);
        assert_eq!(&png[..8], &[137, 80, 78, 71, 13, 10, 26, 10]);

        let mut chunks: Vec<([u8; 4], Vec<u8>)> = Vec::new();
        let mut pos = 8usize;
        while pos < png.len() {
            let length = u32::from_be_bytes([
                png[pos], png[pos + 1], png[pos + 2], png[pos + 3],
            ]) as usize;
            pos += 4;
            let mut kind = [0u8; 4];
            kind.copy_from_slice(&png[pos..pos + 4]);
            pos += 4;
            let data = png[pos..pos + length].to_vec();
            pos += length;
            let mut checked = kind.to_vec();
            checked.extend_from_slice(&data);
            let crc = u32::from_be_bytes([
                png[pos], png[pos + 1], png[pos + 2], png[pos + 3],
            ]);
            pos += 4;
            assert_eq!(crc, crc32(&checked), "bad CRC in {}", String::from_utf8_lossy(&kind));
            chunks.push((kind, data));
        }
        assert_eq!(chunks.len(), 3);
        assert_eq!(&chunks[0].0, b"IHDR");
        assert_eq!(&chunks[1].0, b"IDAT");
        assert_eq!(&chunks[2].0, b"IEND");
        assert_eq!(&chunks[0].1[..4], &3u32.to_be_bytes());
        assert_eq!(&chunks[0].1[4..8], &2u32.to_be_bytes());

        // Unpack the stored DEFLATE blocks: no compressor to trust here.
        let idat = &chunks[1].1;
        assert_eq!(&idat[..2], &[0x78, 0x01]);
        let mut cursor = 2usize;
        let mut scanlines = Vec::new();
        loop {
            let header = idat[cursor];
            cursor += 1;
            let n = u16::from_le_bytes([idat[cursor], idat[cursor + 1]]) as usize;
            cursor += 4; // LEN + NLEN
            scanlines.extend_from_slice(&idat[cursor..cursor + n]);
            cursor += n;
            if header & 1 != 0 {
                break;
            }
        }
        assert_eq!(cursor + 4, idat.len(), "Adler-32 trailer missing");

        let mut expected = Vec::new();
        for row in rgb.chunks_exact(width * 3) {
            expected.push(0u8);
            expected.extend_from_slice(row);
        }
        assert_eq!(scanlines, expected);
    }

    #[test]
    fn window_state_flags_round_trip() {
        let directory = std::env::temp_dir().join(format!("autofind-test-{}", std::process::id()));
        let _ = fs::create_dir_all(&directory);
        std::env::set_var("AUTOFIND_HOME", &directory);
        let file = directory.join("window.txt");

        fs::write(&file, "10 20 900 700 1 1 1").unwrap();
        let state = read_window_state();
        assert_eq!(state.rect, (10, 20, 900, 700));
        assert!(state.always_on_top);
        assert!(state.start_minimized);
        assert!(state.quiet);

        write_window_state((11, 21, 901, 701), false);
        assert_eq!(
            fs::read_to_string(&file).unwrap(),
            "11 21 901 701 0 1 1",
            "writing geometry must not clobber the two flags"
        );

        fs::write(&file, "1 2 800 600 0").unwrap(); // legacy five-field file
        let legacy = read_window_state();
        assert_eq!(legacy.rect, (1, 2, 800, 600));
        assert!(!legacy.start_minimized && !legacy.quiet);

        std::env::remove_var("AUTOFIND_HOME");
        let _ = fs::remove_dir_all(&directory);
    }
}
