#!/usr/bin/env python3
"""
Generates a tailored, interactive Web Preview & Device Staging Portal in `build/web/index.html`
for each repository in the Antigravity portfolio (AMPtech-Hub, Atari-TV, River-Ride,
price-tracker, Mamali-IPTV, SpeakConfidentTV, Home-Painting-Marketplace-, gcp-wif-foundation, etc.).
"""
import os
from pathlib import Path


APP_CONFIGS = {
    "amptech-hub": {
        "badge": "Android TV Leanback Launcher • Play Console (v2.4.0)",
        "accent": "#6366f1",
        "subtitle": "Interactive 10-foot Android TV Launcher showcasing Our Play Console Apps, Watch Insights, and Antigravity AI Hub.",
        "screen_title": "📺 AMPtech Hub — 'Our Apps' & Settings Showcase",
        "package": "com.mamali.amptechhub",
        "cards": [
            ("🕹️", "Atari TV", "Classic Atari 2600 • INSTALLED ✓", "#c62828", "Launches com.mamali.ataritv on TV"),
            ("📺", "Mamali IPTV", "v3.1 Clean-room IPTV • INSTALLED ✓", "#0d47a1", "Launches com.mamali.iptvplayer on TV"),
            ("🏷️", "PriceGuard AI", "AI Price Guide • Phone Only", "#2e7d32", "Shows TV-friendly Phone-Only compatibility dialog"),
            ("🎨", "HomePainting", "Instant Paint Estimate • Phone Only", "#e65100", "Shows TV-friendly Phone-Only compatibility dialog"),
            ("🚀", "Antigravity AI", "AI HUB • Build & Preview All 10 Projects", "#0284c7", "Opens embedded OrchestratorActivity"),
        ],
    },
    "atari-tv": {
        "badge": "Android TV NDK Emulator • Stella 2023 + LibretroDroid",
        "accent": "#ef4444",
        "subtitle": "10-foot Android TV Atari 2600 Console & ROM Shelf with live CRT shader toggle and 5-slot state picker.",
        "screen_title": "🕹️ Atari-TV — 10-Foot ROM Shelf & CRT Emulator",
        "package": "com.mamali.ataritv",
        "cards": [
            ("✈️", "River Raid", "Activision (1982) • Slot 1 Saved", "#0284c7", "Launching River Raid in Stella 2023 NDK Core (CRT Shader ON)..."),
            ("🏃", "Pitfall!", "David Crane • Adventure", "#15803d", "Launching Pitfall! in Stella 2023 NDK Core..."),
            ("🟡", "Pac-Man", "Atari / Namco • Arcade", "#ca8a04", "Launching Pac-Man in Stella 2023 NDK Core..."),
            ("🚀", "Asteroids", "Atari Inc. • Vector Shooter", "#dc2626", "Launching Asteroids in Stella 2023 NDK Core..."),
            ("👾", "Space Invaders", "Taito • Classic Arcade", "#7c3aed", "Launching Space Invaders in Stella 2023 NDK Core..."),
        ],
    },
    "river-ride": {
        "badge": "Android & Android TV Retro Arcade Shooter",
        "accent": "#10b981",
        "subtitle": "Classic River Raid vertical-scrolling jet fighter game with fuel depots, bridges, and D-pad/gamepad flight controls.",
        "screen_title": "✈️ River-Ride — Interactive Flight & Fuel Simulator",
        "package": "com.mamali.riverride",
        "cards": [
            ("🛩️", "Sector 1: Bridge", "Speed: Mach 1.2 • Fuel: 92%", "#059669", "Firing missiles at Sector 1 Bridge! +500 PTS"),
            ("⛽", "Fuel Depot", "Fly Over to Refuel Jet", "#0284c7", "Refueling aircraft... Fuel Tank 100%!"),
            ("🚁", "Enemy Chopper", "Patrol Craft • +60 PTS", "#d97706", "Enemy helicopter destroyed! +60 PTS"),
            ("🚢", "River Tanker", "Armored Destroyer • +30 PTS", "#dc2626", "River destroyer eliminated! +30 PTS"),
            ("🏆", "High Scores", "Top Pilot: 48,200 PTS", "#7c3aed", "Viewing Global & Local TV Leaderboards"),
        ],
    },
    "price-tracker": {
        "badge": "Android Mobile AI App • PriceGuard AI",
        "accent": "#22c55e",
        "subtitle": "Real-time AI price comparison, historical price drop alerts, and instant retail deal scoring.",
        "screen_title": "🏷️ PriceGuard AI — Live Deal Scanner & Price History",
        "package": "com.aistudio.priceguard.aiubxk",
        "cards": [
            ("🎧", "Sony WH-1000XM5", "$278 (-30%) • AI Verdict: BUY NOW", "#15803d", "PriceGuard AI: Lowest price in 180 days ($278 vs $399 avg)."),
            ("📺", "LG C4 65\" OLED", "$1,396 (-22%) • Price Drop Alert", "#0369a1", "PriceGuard AI: Target price $1,400 reached!"),
            ("💻", "MacBook Air M3", "$899 (-18%) • Best Buy / Amazon", "#4f46e5", "PriceGuard AI: Strong deal — 18% below MSRP."),
            ("🎮", "PS5 Pro Console", "$699 (MSRP) • AI Verdict: HOLD", "#b45309", "PriceGuard AI: Wait for bundle discount."),
            ("📷", "Barcode Scanner", "Scan Any In-Store Item", "#0f766e", "Opening camera barcode & receipt AI scanner..."),
        ],
    },
    "mamali-iptv": {
        "badge": "Android TV & Mobile IPTV Player (v3.1 / Build 6)",
        "accent": "#3b82f6",
        "subtitle": "Clean-room M3U / Xtream Codes IPTV player for Android TV & mobile with hardware ExoPlayer decoding and EPG guide.",
        "screen_title": "📡 Mamali IPTV v3.1 — Live Channels, EPG & 4K VOD",
        "package": "com.mamali.iptvplayer",
        "cards": [
            ("⚽", "Sports 4K UHD", "LIVE: Champions League • 60fps HEVC", "#1d4ed8", "Tuning ExoPlayer to Sports 4K UHD stream..."),
            ("🎬", "Cinema Premiere", "NOW: Dune: Part Two • Dolby Audio", "#7e22ce", "Tuning ExoPlayer to Cinema Premiere HD..."),
            ("🌍", "World News Live", "24/7 International News Feed", "#0f766e", "Tuning ExoPlayer to World News Live..."),
            ("📅", "EPG TV Guide", "7-Day Electronic Program Guide", "#b45309", "Opening multi-channel EPG timeline grid..."),
            ("⚙️", "Playlist Manager", "M3U / Xtream / Favorites Sync", "#374151", "Opening M3U & Xtream server settings..."),
        ],
    },
    "speakconfidenttv": {
        "badge": "React Native Android TV • AI Speech & Teleprompter",
        "accent": "#a855f7",
        "subtitle": "10-foot Android TV teleprompter, real-time pacing coach, filler-word counter, and vocal confidence analyzer.",
        "screen_title": "🎙️ SpeakConfident TV — Studio Teleprompter & AI Coach",
        "package": "com.mamali.speakconfidenttv",
        "cards": [
            ("📜", "Keynote Script", "135 WPM • Pacing: Optimal ✓", "#6d28d9", "Starting scrolling TV teleprompter at 135 WPM..."),
            ("🎯", "Pitch Deck Mode", "90-Second Executive Pitch Timer", "#0284c7", "Launching 90s Pitch Deck countdown & eye-contact cue..."),
            ("📊", "Confidence Score", "Last Session: 94/100 (0 Filler Words)", "#059669", "Viewing vocal clarity, cadence, and pause analytics..."),
            ("⏱️", "Impromptu Drill", "60-Second Random Topic Generator", "#d97706", "New topic generated: 'The Future of Autonomous Cloud Agents'"),
            ("🎥", "Rehearsal Replay", "Review Timed Transcript & Markers", "#be185d", "Opening rehearsal transcript breakdown..."),
        ],
    },
    "home-painting-marketplace-": {
        "badge": "Dual Android Apps & Web Marketplace • Homeowner & Painter",
        "accent": "#f97316",
        "subtitle": "Connects homeowners needing instant AI room painting quotes with verified local painting contractors.",
        "screen_title": "🎨 HomePainting Marketplace — Instant Quote & Painter Bids",
        "package": "com.mamali.homepainting.homeowner_app",
        "cards": [
            ("🏠", "3-Bedroom Interior", "Instant Estimate: $1,850 • 2 Days", "#ea580c", "Homeowner App: Viewing breakdown (Walls, Trim, 2 Coats Sherwin-Williams)..."),
            ("🖌️", "Exterior Stucco", "Instant Estimate: $3,400 • 4 Bids", "#0284c7", "Homeowner App: Comparing 4 verified painter bids..."),
            ("🎨", "Color Visualizer", " Emerald Sage • Warm Alabaster • Navy", "#059669", "Previewing paint swatches on living room wall..."),
            ("👷", "Painter Job Board", "12 Active Jobs Within 15 km", "#7c3aed", "Painter App (com.mamali.homepainting.painter): Submitting bid..."),
            ("✅", "Escrow & Milestones", "100% Satisfaction Protected", "#0f766e", "Viewing project milestone schedule & warranty..."),
        ],
    },
    "gcp-wif-foundation": {
        "badge": "Terraform & GCP IAM • Keyless Workload Identity Federation",
        "accent": "#06b6d4",
        "subtitle": "Zero-secret OIDC authentication bridge between GitHub Actions (`aminniktash2010/*`), GCP Cloud Run, and Google Play Console.",
        "screen_title": "🔐 GCP WIF Foundation — Live OIDC Pool & IAM Bindings",
        "package": "project-51459e90-fbb3-471f-9f9",
        "cards": [
            ("🆔", "WIF Pool", "github-actions-pool • ACTIVE", "#0891b2", "OIDC Pool: projects/202076903469/locations/global/workloadIdentityPools/github-actions-pool"),
            ("🔑", "Service Account", "play-upload@project-51459e90...", "#059669", "Roles: run.admin, storage.admin, secretmanager.admin, androidpublisher"),
            ("📦", "Play Console API", "Android Publisher v3 Connected", "#4f46e5", "Keyless OIDC token exchange verified for all Play Console tracks"),
            ("☁️", "Terraform State", "gs://project-51459e90...-tfstate", "#d97706", "Remote GCS state lock & drift detection active"),
            ("🛡️", "Zero JSON Keys", "Org Policy Compliant (100% Keyless)", "#be185d", "All 10 repositories authenticate via short-lived OIDC tokens"),
        ],
    },
}


def generate_portal():
    repo_name = os.environ.get("GITHUB_REPOSITORY_NAME") or os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "App"
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "1")
    commit_sha = (os.environ.get("GITHUB_SHA") or "HEAD")[:7]
    
    out_dir = Path("build/web")
    out_dir.mkdir(parents=True, exist_ok=True)

    key = repo_name.lower()
    cfg = APP_CONFIGS.get(key, {
        "badge": f"{repo_name} • Live Cloud Staging Preview",
        "accent": "#38bdf8",
        "subtitle": f"Interactive browser preview and OTA device installer for {repo_name}.",
        "screen_title": f"📱 {repo_name} — Interactive Screen Preview",
        "package": f"com.mamali.{key.replace('-', '')}",
        "cards": [
            ("🚀", f"{repo_name} Home", "Main Dashboard Screen", "#0284c7", f"Interacting with {repo_name} main screen..."),
            ("⚡", "Live Actions", "Core Workflow Module", "#059669", f"Testing {repo_name} workflow..."),
            ("⚙️", "Settings & Sync", "Cloud Configuration", "#7c3aed", f"Opening {repo_name} settings..."),
        ],
    })

    cards_html = []
    for idx, (icon, title, genre, color, action_msg) in enumerate(cfg["cards"]):
        active_cls = " active" if idx == 0 else ""
        cards_html.append(f"""
          <div class="game-card{active_cls}" data-msg="{action_msg}">
            <div class="poster" style="background: linear-gradient(135deg, {color}, #0f172a);">
              <span style="font-size:2.2rem;margin-bottom:0.4rem;">{icon}</span>
              <span>{title}</span>
            </div>
            <p class="game-title">{title}</p>
            <span class="game-genre">{genre}</span>
          </div>""")

    cards_joined = "\n".join(cards_html)
    first_msg = cfg["cards"][0][4]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{repo_name} - Live Interactive Preview</title>
  <style>
    :root {{
      --bg: #090d16;
      --card: #131b2e;
      --accent: {cfg["accent"]};
      --accent-glow: rgba(56, 189, 248, 0.25);
      --text: #f8fafc;
      --muted: #94a3b8;
      --success: #10b981;
      --border: #23304c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 1.75rem 1rem;
    }}
    .header {{
      text-align: center;
      max-width: 820px;
      margin-bottom: 1.75rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      background: rgba(255, 255, 255, 0.07);
      color: var(--accent);
      border: 1px solid var(--accent);
      margin-bottom: 0.85rem;
    }}
    h1 {{
      margin: 0 0 0.5rem;
      font-size: 1.9rem;
      color: var(--text);
    }}
    p.subtitle {{
      color: var(--muted);
      margin: 0;
      font-size: 0.98rem;
      line-height: 1.45;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 1.5rem;
      max-width: 1040px;
      width: 100%;
    }}
    @media (min-width: 860px) {{
      .grid {{ grid-template-columns: 1.3fr 0.7fr; }}
    }}
    .panel {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.5rem;
      box-shadow: 0 20px 40px rgba(0,0,0,0.4);
    }}
    .panel h2 {{
      margin-top: 0;
      font-size: 1.18rem;
      color: var(--accent);
    }}
    .tv-shelf {{
      background: #060911;
      border: 1px solid #1a233a;
      border-radius: 12px;
      padding: 1.15rem;
      overflow: hidden;
    }}
    .carousel {{
      display: flex;
      gap: 0.9rem;
      overflow-x: auto;
      scroll-behavior: smooth;
      padding: 0.5rem 0.25rem 0.85rem;
    }}
    .game-card {{
      flex: 0 0 165px;
      background: #0e1526;
      border: 2px solid transparent;
      border-radius: 10px;
      padding: 0.7rem;
      text-align: center;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .game-card:hover, .game-card.active {{
      border-color: var(--accent);
      transform: translateY(-4px);
      box-shadow: 0 8px 20px rgba(0,0,0,0.5);
    }}
    .poster {{
      width: 100%;
      height: 155px;
      border-radius: 6px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      margin-bottom: 0.65rem;
      font-weight: 700;
      color: #f8fafc;
      font-size: 0.92rem;
      padding: 0.5rem;
      text-align: center;
    }}
    .game-title {{
      font-size: 0.88rem;
      font-weight: 600;
      margin: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .game-genre {{
      font-size: 0.73rem;
      color: var(--muted);
      margin-top: 0.25rem;
      display: block;
    }}
    .live-output {{
      margin-top: 1rem;
      padding: 0.85rem 1rem;
      border-radius: 8px;
      background: #090e1a;
      border: 1px solid var(--border);
      color: var(--success);
      font-family: monospace;
      font-size: 0.85rem;
    }}
    .dpad-controls {{
      margin-top: 0.75rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.8rem;
      color: var(--muted);
    }}
    .dpad-controls kbd {{
      background: #172033;
      color: var(--text);
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      border: 1px solid var(--border);
    }}
    .qr-box {{
      text-align: center;
      background: #ffffff;
      padding: 0.85rem;
      border-radius: 12px;
      display: inline-block;
      margin: 0.75rem 0;
    }}
    .qr-box img {{
      display: block;
      width: 145px;
      height: 145px;
    }}
    .btn {{
      display: block;
      width: 100%;
      padding: 0.8rem;
      border-radius: 8px;
      font-weight: 600;
      font-size: 0.9rem;
      text-align: center;
      text-decoration: none;
      margin-bottom: 0.65rem;
      cursor: pointer;
      border: none;
    }}
    .btn-primary {{
      background: var(--accent);
      color: #090d16;
    }}
    .btn-secondary {{
      background: #1e293b;
      color: var(--text);
      border: 1px solid var(--border);
    }}
    .meta-list {{
      list-style: none;
      padding: 0;
      margin: 0.85rem 0 0;
      font-size: 0.82rem;
    }}
    .meta-list li {{
      display: flex;
      justify-content: space-between;
      padding: 0.38rem 0;
      border-bottom: 1px solid #1a2438;
      color: var(--muted);
    }}
    .meta-list span.val {{
      color: var(--text);
      font-family: monospace;
    }}
  </style>
</head>
<body>

  <div class="header">
    <div class="badge">{cfg["badge"]}</div>
    <h1>{repo_name}</h1>
    <p class="subtitle">{cfg["subtitle"]}</p>
  </div>

  <div class="grid">
    <div class="panel">
      <h2>{cfg["screen_title"]}</h2>
      <div class="tv-shelf">
        <div class="carousel" id="carousel">
{cards_joined}
        </div>

        <div id="sim-status" class="live-output">▶ {first_msg}</div>

        <div class="dpad-controls">
          <span>Click any card or use D-Pad / Arrow Keys:</span>
          <div><kbd>◀</kbd> <kbd>▶</kbd> &nbsp; Action: <kbd>Enter</kbd></div>
        </div>
      </div>
    </div>

    <div class="panel">
      <h2>📲 Package &amp; Play Console Gate</h2>
      <div style="text-align: center;">
        <div class="qr-box">
          <img src="https://api.qrserver.com/v1/create-qr-code/?size=145x145&data=https://github.com/aminniktash2010/{repo_name}/actions" alt="QR Code" />
        </div>
      </div>

      <a href="https://github.com/aminniktash2010/{repo_name}/actions" class="btn btn-primary" target="_blank">
        ✅ View CI Build &amp; Play Console Gate
      </a>

      <a href="https://github.com/aminniktash2010/{repo_name}" class="btn btn-secondary" target="_blank">
        📂 Open {repo_name} GitHub Repository
      </a>

      <ul class="meta-list">
        <li><span>Repository</span> <span class="val">aminniktash2010/{repo_name}</span></li>
        <li><span>Package / ID</span> <span class="val">{cfg["package"]}</span></li>
        <li><span>Build / Commit</span> <span class="val">#{run_number} ({commit_sha})</span></li>
        <li><span>Play Track</span> <span class="val" style="color:var(--success)">Internal Test</span></li>
      </ul>
    </div>
  </div>

  <script>
    const cards = document.querySelectorAll('.game-card');
    const statusBox = document.getElementById('sim-status');
    let currentIndex = 0;

    function setActive(index) {{
      cards.forEach(c => c.classList.remove('active'));
      currentIndex = (index + cards.length) % cards.length;
      const card = cards[currentIndex];
      card.classList.add('active');
      card.scrollIntoView({{ behavior: 'smooth', inline: 'center', block: 'nearest' }});
      statusBox.textContent = '▶ ' + (card.getAttribute('data-msg') || card.querySelector('.game-title').textContent);
    }}

    window.addEventListener('keydown', (e) => {{
      if (e.key === 'ArrowRight' || e.key === 'Right') setActive(currentIndex + 1);
      else if (e.key === 'ArrowLeft' || e.key === 'Left') setActive(currentIndex - 1);
      else if (e.key === 'Enter') setActive(currentIndex);
    }});

    cards.forEach((card, idx) => {{
      card.addEventListener('click', () => setActive(idx));
    }});
  </script>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"Generated tailored web preview portal for {repo_name} in {out_dir / 'index.html'}")


if __name__ == "__main__":
    generate_portal()
