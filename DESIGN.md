# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-09-01
- Primary product surfaces: authenticated desktop/mobile file browser, directory list, favorites navigation, file preview, upload status
- Evidence reviewed:
  - `README.md` / `README.zh-CN.md` — bilingual single-user positioning, supported previews, responsive behavior, and deployment instructions
  - `web_file_manager.py:609` — all frontend HTML, CSS, and JavaScript are embedded in one Python file
  - `web_file_manager.py:621` — Cloud Light is forced as the active application theme
  - `web_file_manager.py:1593` — canonical light shell, navigation, file-row, preview, and responsive CSS
  - `web_file_manager.py` — unified resizable browser sidebar containing collapsed Favorites, path controls, and the current directory
  - `web_file_manager.py:2299` — flexible preview pane with persistent metadata/actions header
  - `web_file_manager.py:2639` — favorites render as stable navigation rather than list content
  - `web_file_manager.py:2908` — preview empty/selected state is explicit and mobile-aware
  - `docs/screenshots/web-file-manager-demo.png` — 1440×900 desktop baseline captured from an isolated temporary HOME with fictional content
- The original repository had no design brief, screenshot baseline, logo, or brand asset; this file now owns the design contract and the sanitized desktop baseline above.

## Brand

- Personality: calm, capable, personal, desktop-native, and trustworthy; closer to a focused productivity tool than a server admin dashboard.
- Trust signals: predictable file paths, clear selected/current states, restrained color, visible file metadata, obvious download/upload actions, and legible destructive states.
- Avoid: editor-theme cosplay, large gradients, dashboard cards, glassmorphism, persistent emoji iconography, novelty animations, mixed-language labels, and controls that appear only on hover.

## Product goals

- Goals:
  - Make the current directory, file list, favorites, and preview visually distinct at a glance.
  - Keep Favorites as the only shortcut section inside the same collapsible browser sidebar as the active directory.
  - Maximize preview space while keeping file metadata and actions consistently available.
  - Use one coherent light visual system by default.
  - Preserve the directness and speed of the existing single-page application.
- Non-goals:
  - Cloud-drive collaboration, sharing, comments, version history, or account management.
  - A large theme gallery in the primary interface.
  - Thumbnail-grid view, global search, or bulk file operations in the first redesign pass.
  - Replacing the existing backend or introducing a frontend build system.
- Success signals:
  - Favorites are collapsed by default and expand only on explicit activation, so the current directory receives maximum vertical space.
  - The selected file and current directory remain obvious without relying on a colored left border alone.
  - Preview actions are visible without hovering over the content.
  - All primary desktop actions are understandable without reading tooltips.
  - Mobile users navigate with explicit back/drawer controls rather than horizontal pane discovery.

## Personas and jobs

- Primary personas: one authenticated owner browsing files across a workstation or server, usually through LAN/Tailscale.
- User jobs:
  - Jump to frequently used directories.
  - Understand the current path and move upward quickly.
  - Scan filenames, size, and modification time efficiently.
  - Preview images, video, PDF, Markdown, HTML, and text without downloading first.
  - Upload or download files with clear feedback.
- Key contexts of use: large desktop monitor for sustained browsing; phone for quick lookup and preview; potentially slow remote connection for large media.

## Information architecture

- Primary navigation:
  - Desktop uses two functional regions: one unified browser sidebar and one preview pane.
  - The browser sidebar contains product identity, a collapsed-by-default Favorites section, current-path controls, and the active directory list in one vertical surface.
  - Do not duplicate Home/Locations above the breadcrumb; the breadcrumb root and Up action already provide that navigation.
  - The entire browser sidebar can collapse to the left edge; a visible control in the preview header restores it.
  - Favorites are pinned locations; starring a directory changes navigation, not the layout of the active directory.
- Core routes/screens:
  - Retain the single `/` route and existing deep-link behavior.
  - File preview remains in-context on desktop and becomes a full-screen detail surface on narrow screens.
- Content hierarchy:
  1. Current location and selected file
  2. File list and primary actions
  3. Favorites shortcuts within the browser sidebar
  4. Secondary preferences such as hidden files, sorting, and appearance

Recommended desktop shell:

```text
┌───────────────────────────────────────────────┬──────────────────────────────────┐
│ Files                              Collapse   │ report.pdf            Download  │
│ FAVORITES ▸                                   ├──────────────────────────────────┤
├───────────────────────────────────────────────┤                                  │
│ Home / Projects                               │                                  │
│ ↑  Refresh              Sort        Upload    │          File preview            │
├───────────────────────────────────────────────┤                                  │
│ Folder A                                  2h  │                                  │
│ report.pdf                            1.2 MB  │                                  │
│ clip.mp4                               84 MB  │                                  │
│ notes.md                                8 KB  │                                  │
└───────────────────────────────────────────────┴──────────────────────────────────┘
            360–900 px resizable                         flexible
```

## Design principles

- Structure before decoration: use spatial separation and typography to explain the application; color only reinforces state.
- Navigation is stable: Favorites do not move when the current directory changes, while root navigation remains in the breadcrumb.
- Content stays unobscured: filenames, actions, and warnings live in headers or status areas rather than overlays on previews.
- Familiar file-manager behavior: explicit back/up actions, row selection, predictable context actions, and a clear active path.
- Progressive disclosure: keep Upload and path navigation visible; move hidden-file toggle, theme choice, and less frequent actions into a compact menu/settings surface.
- Tradeoffs: merging navigation and files saves horizontal space; the bounded shortcut section uses some vertical space but keeps all browsing controls in one predictable surface.

## Visual language

- Color: use the `Cloud Light` palette as the default and primary supported appearance.
  - Canvas: `#F5F7FA`
  - Navigation surface: `#F8FAFC`
  - Primary surface: `#FFFFFF`
  - Border/divider: `#E4E7EC`
  - Primary text: `#101828`
  - Secondary text: `#667085`
  - Accent: `#2563EB`; hover `#1D4ED8`
  - Selection: `#EAF2FF`; selection text `#1849A9`
  - Focus ring: `#84ADFF`
  - Success: `#039855`; warning: `#DC6803`; danger: `#D92D20`
- Typography: system UI stack only; 14 px body, 13 px controls/metadata, 12 px section labels, 16–18 px pane titles; filenames use medium weight only when selected.
- Spacing/layout rhythm: 4 px base unit; 8/12 px control spacing; 16 px pane padding where content is not row-based; 40–44 px file and navigation rows.
- Shape/radius/elevation: 8 px controls, 10 px menus/dialogs; panes remain flat with 1 px dividers; shadows are reserved for floating menus and dialogs.
- Motion: 120–180 ms ease-out for hover, selection, drawer, and resize affordances; no content entrance animation.
- Imagery/iconography: replace emoji with consistent 18 px inline SVG outline icons using `currentColor`; file-type color may appear as a small muted accent, not a full colored emoji.

## Components

- Existing components to reuse:
  - Current file APIs, path resolution, favorites data, deep links, upload logic, preview renderers, and file-stream behavior.
  - Existing sidebar width persistence can be adapted to the directory-list column.
  - Existing CSS-variable mechanism remains the token surface.
- New/changed components:
  - `AppShell`: two-region desktop layout and single-surface mobile layout.
  - `BrowserSidebar`: one collapsible/resizable surface containing product label, collapsed-by-default Favorites, secondary preferences, path controls, and the file list.
  - `FavoritesDisclosure`: one accessible button with count and chevron; its controlled list is hidden initially and expands in place without changing sidebar width.
  - `HiddenFilesToggle`: closed-eye icon means hidden files are not shown; open-eye icon means they are visible. Mirror the visual state with status text, `aria-pressed`, and an action-oriented accessible name.
  - `DirectoryHeader`: breadcrumb/path editor on one row; primary actions on a compact adjacent row or right side.
  - `FileList`/`FileRow`: aligned file-type icon, filename, metadata, and trailing favorite action shown on row hover/focus for directories.
  - `PreviewHeader`: persistent filename, size/type metadata, Source toggle where applicable, and Download action.
  - `PreviewCanvas`: content-only preview area with no metadata overlay.
  - `ActionMenu`: sorting, hidden-files toggle, and secondary preferences.
  - `UploadStatus`: bottom status strip in the file-list pane rather than content that shifts the list.
- Variants and states:
  - Navigation/file rows: default, hover, selected/current, keyboard focus, disabled, and pending favorite update.
  - Icon buttons: neutral, primary, danger, active-toggle, and disabled.
  - Preview: empty, loading, ready, unsupported, and error.
- Token/component ownership: tokens stay under `:root`; component selectors consume semantic tokens and must not embed theme-specific colors except media canvas black.

## Accessibility

- Target standard: WCAG 2.1 AA for contrast, focus visibility, and keyboard operation.
- Keyboard/focus behavior:
  - Every icon button has an accessible name and visible focus ring.
  - File rows and favorite rows are keyboard reachable and activate with Enter/Space.
  - When a video is selected, Left/Right Arrow seeks backward/forward by 5 seconds and clamps at the media bounds.
  - Focus returns to the selected row when closing a mobile preview or menu.
  - Existing `b` sidebar shortcut may remain, but visible controls must provide the same operation.
- Contrast/readability: secondary text must maintain at least 4.5:1 at small sizes; selection is conveyed by background plus text/icon treatment, not color alone.
- Screen-reader semantics: use landmarks (`nav`, `main`), buttons for actions, list/listitem semantics for files and favorites, and live regions for upload/error status.
- Reduced motion and sensory considerations: honor `prefers-reduced-motion`; preview videos start muted; never depend solely on hover or animation to reveal required actions.

## Responsive behavior

- Supported breakpoints/devices:
  - `>= 821 px`: resizable split view; the unified browser sidebar can collapse completely and the preview expands to fill the window.
  - `< 820 px`: one active surface at a time.
- Layout adaptations:
  - Mobile opens on the unified browser sidebar, with Favorites collapsed above the file list; selecting a file pushes a full-screen preview with a Back button.
  - Remove horizontal whole-pane scroll snapping; it hides the navigation model and conflicts with media/document gestures.
  - Preview headers stay pinned on mobile; Download moves into the overflow menu if width is constrained.
- Touch/hover differences: trailing row actions remain visible on touch-selected rows; all touch targets are at least 40 px, preferably 44 px.

## Interaction states

- Loading: preserve the current pane and show a subtle inline spinner/skeleton in the region being refreshed; avoid blanking the entire application.
- Background refresh: retain the selected file and its existing preview DOM when the file revision is unchanged, so images remain visible and videos keep playback state; rebuild or clear only when the file changes or disappears.
- Empty: explain whether a directory is empty, filtered by hidden-file settings, or has no favorites; provide the relevant next action.
- Error: show an inline, dismissible error in the affected pane with retry; reserve browser alerts for unrecoverable fallback only.
- Success: upload completion appears briefly in the status strip and the list refreshes without shifting layout.
- Disabled: reduce contrast while preserving label legibility; explain unavailable actions with tooltip/help text where needed.
- Offline/slow network: keep loaded navigation/list content visible, show media loading state, and surface failed Range/preview requests without losing selection.

## Content voice

- Tone: concise, calm, and literal.
- Terminology: use one interface language consistently. The proposed implementation uses Simplified Chinese (`文件`, `收藏夹`, `上传`, `下载`, `排序`, `显示隐藏文件`) while preserving filenames verbatim.
- Microcopy rules: use verbs for actions, nouns for sections, sentence case, and no cryptic labels such as `排:名↑` or `·on`.

## Implementation constraints

- Framework/styling system: retain Tornado plus embedded vanilla HTML/CSS/JavaScript in `web_file_manager.py`; no build step.
- Design-token constraints:
  - Introduce semantic light tokens rather than editing each of the existing twelve palettes independently.
  - `Cloud Light` is the only user-facing theme. Legacy saved theme values are intentionally ignored.
  - Code preview uses the light GitHub highlight.js stylesheet.
- Performance constraints: no new font, icon, or UI-framework network dependency; inline SVG icons and existing local assets only.
- Compatibility constraints: preserve Basic Auth, deep links, keyboard image navigation, media Range requests, PDF.js, MathJax, and all current file operations.
- Markdown rendering: typeset common inline/display LaTeX delimiters with the vendored MathJax bundle after each rendered-view update; source mode remains literal and code blocks are excluded from typesetting.
- Test/screenshot expectations:
  - Functional checks for favorites navigation, directory loading, file selection, upload, theme migration, and preview actions.
  - Visual baselines at 1440×900, 1024×768, and 390×844.
  - README screenshots must be captured from an isolated temporary HOME with fictional files and audited for usernames, absolute paths, hostnames, and personal favorites before commit.
  - Keyboard/focus and reduced-motion checks before release.

## Open questions

- [x] Three-region desktop layout approved and implemented on 2026-08-03, then superseded by the unified two-region sidebar on 2026-08-31.
- [x] Simplified Chinese adopted for primary interface labels on 2026-08-03.
- [x] Dark/theme-gallery controls removed from the user interface; Cloud Light is canonical.
- [x] Locations/Favorites and the active directory were merged into one collapsible sidebar on 2026-08-31.
- [x] The redundant Locations section was removed and Favorites became collapsed-by-default on 2026-08-31.
