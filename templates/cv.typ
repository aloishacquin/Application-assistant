// CV template, driven by data.json (written by jobapply.generate.render).
// One column, standard fonts, no ligatures/hyphenation: text extracts cleanly for ATS.
#let data = json("data.json")

#set document(title: data.name + " – CV", author: data.name)
#set page(paper: "a4", margin: (x: 1.6cm, y: 1.4cm))
#set text(
  font: ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans", "Libertinus Serif"),
  size: 10pt,
  lang: "en",
  ligatures: false,
  hyphenate: false,
)
#set par(justify: false, leading: 0.55em)
#set list(indent: 0.4em, body-indent: 0.5em, spacing: 0.45em)

#let accent = rgb("#1f3a68")

#align(center)[
  #text(size: 20pt, weight: "bold", fill: accent)[#data.name] \
  #v(2pt)
  #text(size: 9.5pt)[#data.contact.join("  |  ")]
]
#v(4pt)
#align(center)[#text(size: 10.5pt, style: "italic")[#data.headline]]

#let section(title) = {
  v(8pt)
  text(size: 11.5pt, weight: "bold", fill: accent)[#upper(title)]
  v(-6pt)
  line(length: 100%, stroke: 0.6pt + accent)
  v(2pt)
}

#for sec in data.sections {
  section(sec.title)
  if "entries" in sec {
    for entry in sec.entries {
      grid(
        columns: (1fr, auto),
        text(weight: "bold")[#entry.title],
        align(right)[#entry.dates],
      )
      if entry.subtitle != "" {
        v(-4pt)
        text(style: "italic")[#entry.subtitle]
      }
      if entry.bullets.len() > 0 {
        v(-2pt)
        list(..entry.bullets.map(b => [#b]))
      }
      v(4pt)
    }
  } else {
    for row in sec.rows [
      *#row.label:* #row.value \
    ]
  }
}
