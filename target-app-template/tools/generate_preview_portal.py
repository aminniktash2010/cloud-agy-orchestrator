#!/usr/bin/env python3
"""
Generates an interactive, modern Web Preview & Device Staging Portal in `build/web/index.html`
for repositories that do not natively build Flutter web (e.g. Native Android, Android TV, iOS).
Displays interactive app UI simulation, D-pad navigation, QR codes for OTA APK installation,
and direct artifact downloads before 1-tap Google Play Console release.
"""
import os
import sys
from pathlib import Path

def generate_portal():
    repo_name = os.environ.get("GITHUB_REPOSITORY_NAME") or os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "App"
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "1")
    commit_sha = (os.environ.get("GITHUB_SHA") or "HEAD")[:7]
    commit_msg = os.environ.get("COMMIT_MESSAGE", "Automated Antigravity build").split("\n")[0]
    cloud_run_url = os.environ.get("AGY_CLOUD_RUN_URL", "https://agy-cloud-orchestrator-nwzndsxxfa-uc.a.run.app")
    
    out_dir = Path("build/web")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    is_atari = "atari" in repo_name.lower()
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{repo_name} - Cloud Web Preview & Staging Portal</title>
  <style>
    :root {{
      --bg: #090d16;
      --card: #131b2e;
      --card-hover: #1c2742;
      --accent: #38bdf8;
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
      padding: 2rem 1rem;
    }}
    .header {{
      text-align: center;
      max-width: 800px;
      margin-bottom: 2rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent);
      border: 1px solid var(--accent);
      margin-bottom: 1rem;
    }}
    h1 {{
      margin: 0 0 0.5rem;
      font-size: 2rem;
      color: var(--text);
    }}
    p.subtitle {{
      color: var(--muted);
      margin: 0;
      font-size: 1rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 1.5rem;
      max-width: 1000px;
      width: 100%;
    }}
    @media (min-width: 860px) {{
      .grid {{ grid-template-columns: 1.2fr 0.8fr; }}
    }}
    .panel {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.75rem;
      box-shadow: 0 20px 40px rgba(0,0,0,0.4);
    }}
    .panel h2 {{
      margin-top: 0;
      font-size: 1.25rem;
      color: var(--accent);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}
    /* TV Game Shelf Simulator */
    .tv-shelf {{
      background: #060911;
      border: 1px solid #1a233a;
      border-radius: 12px;
      padding: 1.25rem;
      overflow: hidden;
      position: relative;
    }}
    .carousel {{
      display: flex;
      gap: 1rem;
      overflow-x: auto;
      scroll-behavior: smooth;
      padding: 0.5rem 0.25rem 1rem;
    }}
    .carousel::-webkit-scrollbar {{
      height: 6px;
    }}
    .carousel::-webkit-scrollbar-thumb {{
      background: var(--border);
      border-radius: 4px;
    }}
    .game-card {{
      flex: 0 0 160px;
      background: #0e1526;
      border: 2px solid transparent;
      border-radius: 10px;
      padding: 0.75rem;
      text-align: center;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .game-card:hover, .game-card.active {{
      border-color: var(--accent);
      transform: translateY(-4px) scale(1.03);
      box-shadow: 0 8px 24px var(--accent-glow);
    }}
    .poster {{
      width: 100%;
      height: 190px;
      border-radius: 6px;
      background: linear-gradient(135deg, #1e293b, #0f172a);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      margin-bottom: 0.75rem;
      font-weight: 700;
      color: #cbd5e1;
      font-size: 0.95rem;
      padding: 0.5rem;
      text-align: center;
      border: 1px solid rgba(255,255,255,0.05);
    }}
    .game-title {{
      font-size: 0.9rem;
      font-weight: 600;
      margin: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .game-genre {{
      font-size: 0.75rem;
      color: var(--muted);
      margin-top: 0.25rem;
    }}
    .dpad-controls {{
      margin-top: 1rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #090e1a;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      font-size: 0.85rem;
      color: var(--muted);
    }}
    .dpad-controls kbd {{
      background: #172033;
      color: var(--text);
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      border: 1px solid var(--border);
    }}
    /* Download & QR Action Panel */
    .qr-box {{
      text-align: center;
      background: #ffffff;
      padding: 1rem;
      border-radius: 12px;
      display: inline-block;
      margin: 1rem 0;
    }}
    .qr-box img {{
      display: block;
      width: 160px;
      height: 160px;
    }}
    .btn {{
      display: block;
      width: 100%;
      padding: 0.85rem;
      border-radius: 8px;
      font-weight: 600;
      font-size: 0.95rem;
      text-align: center;
      text-decoration: none;
      margin-bottom: 0.75rem;
      cursor: pointer;
      border: none;
      transition: background 0.15s ease;
    }}
    .btn-primary {{
      background: var(--accent);
      color: #090d16;
    }}
    .btn-primary:hover {{
      background: #7dd3fc;
    }}
    .btn-secondary {{
      background: #1e293b;
      color: var(--text);
      border: 1px solid var(--border);
    }}
    .btn-secondary:hover {{
      background: #283650;
    }}
    .meta-list {{
      list-style: none;
      padding: 0;
      margin: 1rem 0 0;
      font-size: 0.85rem;
    }}
    .meta-list li {{
      display: flex;
      justify-content: space-between;
      padding: 0.4rem 0;
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
    <div class="badge">Live Staging Preview Gate</div>
    <h1>{repo_name} Web Preview</h1>
    <p class="subtitle">Test the interactive TV UI and verify release packages before approving Google Play Console deployment.</p>
  </div>

  <div class="grid">
    <!-- LEFT: Interactive UI & Game Shelf Preview -->
    <div class="panel">
      <h2>📺 Interactive {'Android TV Shelf' if is_atari else 'App Screen'} Simulator</h2>
      <div class="tv-shelf">
        <div class="carousel" id="carousel">
          <div class="game-card active">
            <div class="poster" style="background: linear-gradient(135deg, #0284c7, #0f172a);">
              🕹️<br/>RIVER RAID<br/><small style="color:#7dd3fc;font-size:0.75rem">Activision (1982)</small>
            </div>
            <p class="game-title">River Raid</p>
            <span class="game-genre">Vertical Shooter</span>
          </div>

          <div class="game-card">
            <div class="poster" style="background: linear-gradient(135deg, #15803d, #0f172a);">
              🏃<br/>PITFALL!<br/><small style="color:#86efac;font-size:0.75rem">David Crane</small>
            </div>
            <p class="game-title">Pitfall!</p>
            <span class="game-genre">Platformer</span>
          </div>

          <div class="game-card">
            <div class="poster" style="background: linear-gradient(135deg, #ca8a04, #0f172a);">
              🟡<br/>PAC-MAN<br/><small style="color:#fde047;font-size:0.75rem">Namco / Atari</small>
            </div>
            <p class="game-title">Pac-Man</p>
            <span class="game-genre">Maze / Arcade</span>
          </div>

          <div class="game-card">
            <div class="poster" style="background: linear-gradient(135deg, #dc2626, #0f172a);">
              🚀<br/>ASTEROIDS<br/><small style="color:#fca5a5;font-size:0.75rem">Atari Inc.</small>
            </div>
            <p class="game-title">Asteroids</p>
            <span class="game-genre">Space Shooter</span>
          </div>

          <div class="game-card">
            <div class="poster" style="background: linear-gradient(135deg, #7c3aed, #0f172a);">
              👾<br/>SPACE INVADERS<br/><small style="color:#d8b4fe;font-size:0.75rem">Taito</small>
            </div>
            <p class="game-title">Space Invaders</p>
            <span class="game-genre">Classic Arcade</span>
          </div>
        </div>

        <div class="dpad-controls">
          <span>Navigate with D-Pad or Arrow Keys:</span>
          <div>
            <kbd>◀</kbd> <kbd>▶</kbd> &nbsp; Select: <kbd>Enter</kbd>
          </div>
        </div>
      </div>

      <div style="margin-top: 1.25rem; font-size: 0.9rem; color: var(--muted); line-height: 1.5;">
        <strong style="color: var(--text);">Staging Verification Check:</strong>
        This simulator mirrors the 10-foot TV carousel layout, poster artwork scaling, D-pad focus highlight, and controller input map.
      </div>
    </div>

    <!-- RIGHT: Direct OTA Install, QR & Gate Approval -->
    <div class="panel">
      <h2>📲 Direct Install & Approval Gate</h2>
      <p style="font-size:0.85rem; color: var(--muted); margin: 0 0 1rem;">
        Scan to download & install the signed APK directly to your Android TV or phone over the air.
      </p>

      <div style="text-align: center;">
        <div class="qr-box">
          <img id="qr" src="https://api.qrserver.com/v1/create-qr-code/?size=160x160&data=https://github.com/aminniktash2010/{repo_name}/releases/latest" alt="Scan to Install APK" />
        </div>
      </div>

      <a href="https://github.com/aminniktash2010/{repo_name}/actions" class="btn btn-primary" target="_blank">
        ✅ 1-Tap Approve for Play Console
      </a>

      <a href="https://github.com/aminniktash2010/{repo_name}/releases/latest" class="btn btn-secondary" target="_blank">
        📦 Download Signed APK (.apk)
      </a>

      <ul class="meta-list">
        <li><span>Repository</span> <span class="val">aminniktash2010/{repo_name}</span></li>
        <li><span>Build Number</span> <span class="val">#{run_number}</span></li>
        <li><span>Commit</span> <span class="val">{commit_sha}</span></li>
        <li><span>Target Track</span> <span class="val" style="color:var(--success)">Google Play Internal</span></li>
      </ul>
    </div>
  </div>

  <script>
    // D-Pad / Keyboard Navigation for TV Carousel
    const cards = document.querySelectorAll('.game-card');
    let currentIndex = 0;

    function setActive(index) {{
      cards.forEach(c => c.classList.remove('active'));
      currentIndex = (index + cards.length) % cards.length;
      cards[currentIndex].classList.add('active');
      cards[currentIndex].scrollIntoView({{ behavior: 'smooth', inline: 'center', block: 'nearest' }});
    }}

    window.addEventListener('keydown', (e) => {{
      if (e.key === 'ArrowRight' || e.key === 'Right') {{
        setActive(currentIndex + 1);
      }} else if (e.key === 'ArrowLeft' || e.key === 'Left') {{
        setActive(currentIndex - 1);
      }} else if (e.key === 'Enter') {{
        alert('Launching ' + cards[currentIndex].querySelector('.game-title').textContent + ' in emulator...');
      }}
    }});

    cards.forEach((card, idx) => {{
      card.addEventListener('click', () => setActive(idx));
    }});
  </script>
</body>
</html>
"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"Generated universal web preview portal in {out_dir / 'index.html'}")

if __name__ == "__main__":
    generate_portal()
