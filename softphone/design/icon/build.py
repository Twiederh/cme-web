import subprocess, os
H=open('handset.txt').read().strip()
ARCS='''<path d="M 600 345 A 120 120 0 0 1 690 440"/>
      <path d="M 612 215 A 245 245 0 0 1 820 425" opacity="{op}"/>'''
def glyph(fill, op="0.8", shadow=True):
    f=' filter="url(#sh)"' if shadow else ''
    return f'''<g transform="translate(-30 22)"{f}>
    <path d="{H}" fill="{fill}"/>
    <g fill="none" stroke="{fill}" stroke-linecap="round" stroke-width="56">
      {ARCS.format(op=op)}
    </g></g>'''
DEFS='''<defs>
    <linearGradient id="bg" x1="0.1" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="#2F6BFF"/><stop offset="0.55" stop-color="#1A7FD6"/><stop offset="1" stop-color="#11B3A4"/>
    </linearGradient>
    <linearGradient id="bgd" x1="0.1" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="#16213A"/><stop offset="1" stop-color="#0A1222"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.28" cy="0.18" r="0.75">
      <stop offset="0" stop-color="#fff" stop-opacity="0.30"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="fg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#E6F1FF"/>
    </linearGradient>
    <linearGradient id="fgd" x1="0.1" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="#5B8CFF"/><stop offset="1" stop-color="#2BD4C2"/>
    </linearGradient>
    <linearGradient id="fgt" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#BDBDBD"/>
    </linearGradient>
    <filter id="sh" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="20" stdDeviation="24" flood-color="#052a55" flood-opacity="0.35"/>
    </filter>
    <filter id="msh" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#000" flood-opacity="0.30"/>
    </filter>
  </defs>'''
def svg(body): return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">{DEFS}{body}</svg>'
ios_light = svg('<rect width="1024" height="1024" fill="url(#bg)"/><rect width="1024" height="1024" fill="url(#glow)"/>'+glyph("url(#fg)"))
ios_dark  = svg(glyph("url(#fgd)", shadow=False))           # transparent bg, iOS draws dark
ios_tint  = svg('<rect width="1024" height="1024" fill="#000"/>'+glyph("url(#fgt)", shadow=False))
# macOS: 824er-Squircle auf 1024er Leinwand (Apple-Raster), Inhalt skaliert
inner = '<rect width="1024" height="1024" fill="url(#bg)"/><rect width="1024" height="1024" fill="url(#glow)"/>'+glyph("url(#fg)")
mac = svg(f'''<clipPath id="sq"><rect x="0" y="0" width="1024" height="1024" rx="230"/></clipPath>
  <g filter="url(#msh)"><g transform="translate(100 100) scale(0.8046875)"><g clip-path="url(#sq)">{inner}</g>
  <rect x="2" y="2" width="1020" height="1020" rx="229" fill="none" stroke="#fff" stroke-opacity="0.18" stroke-width="4"/></g></g>''')
for n,s in [("ios_light",ios_light),("ios_dark",ios_dark),("ios_tinted",ios_tint),("mac",mac)]:
    open(n+".svg","w").write(s)
    subprocess.run(["rsvg-convert","-w","1024","-h","1024",n+".svg","-o",n+".png"],check=True)
