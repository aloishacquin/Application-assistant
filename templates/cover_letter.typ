// Cover letter template, driven by data.json (written by jobapply.generate.render).
#let data = json("data.json")

#set document(title: data.subject, author: data.name)
#set page(paper: "a4", margin: (x: 2.2cm, y: 2cm))
#set text(
  font: ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans", "Libertinus Serif"),
  size: 11pt,
  lang: "en",
  ligatures: false,
  hyphenate: false,
)
#set par(justify: false, leading: 0.65em, spacing: 1.1em)

#text(size: 14pt, weight: "bold")[#data.name] \
#for line in data.contact [#line \
]

#v(12pt)
#data.date

#v(4pt)
#data.recipient.join("\n")

#v(12pt)
#text(weight: "bold")[#data.subject]

#v(6pt)
#data.greeting

#for p in data.paragraphs [
  #p

]

#data.closing \
#data.name
