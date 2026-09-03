# Product Design QA

Date: 2026-09-03

## Comparison target

- Source visual truth path (global shell): `D:\project\H3-MotionStudio\design\douzy-shell-reference.png`
- Source visual truth path (generation workspace content): `D:\project\H3-MotionStudio\design\reference-ui.png`
- Implementation URL: `http://127.0.0.1:8111/douyin`
- Implementation screenshot path: Codex task transcript, CUA in-app browser tab 2 final desktop capture
- Additional implementation captures: Codex task transcript, CUA in-app browser tab 3 desktop capture and tab 2 compact-panel capture
- State: dark theme; downloader service connected; Cookie not configured; one failed historical task visible

## Viewport and normalization

- Source shell pixels: 1600 × 1000 at the supplied raster density.
- Final desktop implementation capture: 1900 × 895 JPEG; browser CSS viewport 2253 × 896; device pixel ratio 1.375.
- Additional compact implementation capture: 1150 × 647 JPEG; browser CSS viewport 1164 × 654; device pixel ratio 1.375.
- Narrow panel rendering capture: 520 × 662 JPEG.
- Normalization: full-view captures were compared fit-to-width. The source is an art-direction target rather than a pixel-identical product screen, so Douzy branding, mascot art, TikTok platform tabs, and account/licensing controls are intentionally not copied.

## Full-view comparison evidence

- Fonts and typography: both use a heavy white Chinese display headline, small tracked cyan eyebrow text, muted gray helper copy, and compact controls. The implementation uses the project's system-font stack to preserve Chinese rendering and matching optical hierarchy.
- Spacing and layout rhythm: the desktop implementation preserves the fixed narrow sidebar, centered content column, large hero-to-card gap, thin rounded panels, and generous lower-page whitespace. At the narrow breakpoint the sidebar becomes a compact top navigation with no clipped controls.
- Colors and visual tokens: the implementation matches the source's near-black indigo canvas, slightly lighter panel fill, low-contrast violet borders, cyan focus/primary accents, and muted secondary text. Failure states use restrained rose while retaining text and icon labels.
- Image quality and asset fidelity: source mascot and Douzy logo assets are intentionally excluded because this is H3 MotionStudio, not a Douzy clone. Product controls use the existing Phosphor icon library; no placeholder or improvised CSS illustration is present.
- Copy and content: TikTok-specific copy is correctly localized to 抖音, and the page exposes supported URL types, service/Cookie readiness, recent task state, output path, and completed-video access.

## Focused region comparison evidence

- Download card: input/button proportions, cyan border emphasis, label hierarchy, radius, and helper line match the source visual language while adding real recognition feedback.
- Navigation shell: active route uses cyan outline and glow; secondary entries and system status retain the source's subdued density.
- Error/task states: invalid input produces an accessible alert; failed jobs remain textually distinguishable and the Cookie warning explains the current external dependency.

## Findings

- No actionable P0, P1, or P2 mismatch remains.
- P3: the reference mascot would add personality, but copying it would conflict with H3 MotionStudio branding and was intentionally omitted.

## Comparison history

- Iteration 1: the initial implementation still targeted port 8011, which is reserved by Windows on this machine. Fixed all backend launcher, development script, proxy, and documentation references to 8111; post-fix evidence shows both routes and all tested API endpoints loading successfully.
- Iteration 2: the visible narrow capture confirmed responsive stacking and no clipped controls. The hidden desktop capture confirmed the fixed sidebar, hero, card, notice, and task list proportions against the source shell.

## Primary interactions and runtime checks

- Route navigation between `/` and `/douyin`: passed.
- Invalid URL validation and accessible error alert: passed.
- Valid Douyin URL recognition state: passed.
- Downloader status and job-list loading: passed.
- Browser login launch, waiting, automatic Cookie save, and ready states: passed.
- Real download using the supplied `modal_id` link: passed; MP4 preview and local-download action rendered.
- Browser console errors: none.
- TypeScript typecheck: passed.
- Backend tests: 9 passed.
- Production build and Sites packaging tests: passed.

## Implementation checklist

- [x] Shared reference-led desktop shell across both routes.
- [x] Separate generation and Douyin routes.
- [x] Real downloader service status, submission, polling, history, media lookup, and output-path UI.
- [x] Desktop and narrow responsive validation.
- [x] No actionable P0/P1/P2 visual issues.

final result: passed
