# MuseScore SVG Part Extractor

**Extract individual instrument parts from MuseScore-exported SVG sheet music while preserving the original layout.**

MuseScore SVG Part Extractor is a Python tool that isolates one or more instrument parts from a full musical score exported as SVG. It identifies staves, systems and instruments, then filters the original vector graphics to keep only the selected parts.

It provides both a graphical interface and a command-line interface.

## Features

- **Extract selected instruments** from a full score.
- **Preserve the original layout** by keeping the coordinates of retained SVG elements.
- **List detected instruments** before making a selection.
- **Batch export** one SVG file per instrument.
- **Assemble solo parts across multiple pages** by recomposing the selected instrument's systems into more compact pages.
- **Use a GUI or CLI**, depending on your workflow.

## Requirements

- Python 3.8 or newer.
- Python standard library.
- Tkinter for the graphical interface.
- Optional: PyMuPDF or CairoSVG for the graphical preview.

## Usage

Launch the graphical interface:

```bash
python3 extraire_portee.py
```

List the instruments detected in a score:

```bash
python3 extraire_portee.py score.svg --liste
```

Extract one instrument:

```bash
python3 extraire_portee.py score.svg -i 3
```

Extract multiple instruments:

```bash
python3 extraire_portee.py score.svg -i 1 3
```

Export every instrument into a separate file:

```bash
python3 extraire_portee.py score.svg --tous
```

Assemble one instrument's solo part from multiple SVG pages:

```bash
python3 extraire_portee.py --solo page1.svg page2.svg -i 2
```

## Input and output

The tool is designed for **SVG files exported from MuseScore**. It relies on the vector structure and element classes present in those exports.

It does not directly process MuseScore project files (`.mscz`), PDFs or MusicXML files.

The output consists of SVG files containing the selected musical material.

## When to use this tool

This project is useful when you already have a full-score SVG and want to extract or reassemble instrument parts without manually editing every page or rebuilding the graphical elements.

## Contributing

Bug reports, reproducible examples and contributions that improve instrument detection or compatibility with different MuseScore SVG exports are welcome.

Other information: vibe coded with Haiku 5.5 effort xhigh/max
