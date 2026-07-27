---
name: freecad-cad-design
description: Design, modify, and inspect 3D CAD models in a running FreeCAD instance through the freecad MCP server. Use whenever the user asks to model a part, assembly, bracket, enclosure, gear, flange, or any other geometry in FreeCAD, to edit or measure an existing FreeCAD document, to export STEP/STL, or to run an FEM stress analysis.
---

# FreeCAD CAD design

Drive a live FreeCAD GUI over the `freecad` MCP server. All geometry is
parametric and stays editable in FreeCAD after you are done — prefer document
objects with real properties over one-shot meshes.

## Before touching geometry

1. `list_documents` — see what is open. If the server is unreachable, FreeCAD
   is not running or the RPC server was not started (**FreeCAD MCP → Start RPC
   Server**). Say so instead of retrying blindly.
2. `get_objects(doc_name)` on the target document to learn existing object
   names, types, and placements. Never guess an object name — the *internal*
   `Name` is what the tools take, and it differs from the `Label` shown in the
   tree when a label has been edited.
3. `create_document(name)` only when the user wants a new file.

## The design loop

Build in small steps and verify each one:

**create/edit → `get_object` to confirm properties → `get_view` to confirm it looks right**

`create_object`, `edit_object`, `delete_object`, and `execute_code` already
return a screenshot of the active view, so an extra `get_view` is only needed
for a specific camera angle or a focused close-up:

```
get_view(view_name="Front", focus_object="Bracket")
```

A screenshot that looks empty usually means the geometry is off-camera or has
zero size, not that creation failed — check `get_object` before rebuilding.

## Choosing the right tool

| Situation | Tool |
| --- | --- |
| Primitives, booleans, simple placements | `create_object` / `edit_object` |
| Sketches, PartDesign features, fillets on picked edges, loops, imports, exports, measurements | `execute_code` |
| Long OCCT math (heavy fuse/cut/loft) that would exceed the 90 s GUI budget | `execute_code_async` |
| A stock part (screws, nuts, profiles) | `get_parts_list` then `insert_part_from_library` |

Reach for `create_object` first — it keeps the model readable in the tree. Drop
to `execute_code` as soon as the shape needs a sketch or an edge/face selection.

## create_object properties

Units are **millimetres** and **degrees**. Property names are FreeCAD's own
(`Length`, `Width`, `Height`, `Radius`, `Radius1`, `Angle`, ...) and are
case-sensitive; an unknown property fails the whole call with a message naming
it.

```json
{
  "doc_name": "Bracket",
  "obj_name": "Base",
  "obj_type": "Part::Box",
  "obj_properties": {
    "Length": 80, "Width": 40, "Height": 10,
    "Placement": {
      "Base": {"x": 0, "y": 0, "z": 0},
      "Rotation": {"Axis": {"x": 0, "y": 0, "z": 1}, "Angle": 45}
    },
    "ViewObject": {"ShapeColor": [0.7, 0.7, 0.75]}
  }
}
```

- `Placement` takes `Base` (or `Position`) and `Rotation`; omitted components
  default to 0, and the rotation axis defaults to +Z.
- `ShapeColor` accepts RGB or RGBA floats in 0–1, at the top level or under
  `ViewObject`.
- `Base`, `Tool`, `Source`, and `Profile` accept an **object name string** and
  are resolved to the document object — this is how booleans are wired up.

### Booleans

Create the operands first, then the operation. The operands become children of
the result and stay editable.

```json
{"obj_type": "Part::Cut",   "obj_name": "Pocket", "obj_properties": {"Base": "Base", "Tool": "Cutter"}}
{"obj_type": "Part::Fuse",  "obj_name": "Welded", "obj_properties": {"Base": "Base", "Tool": "Rib"}}
{"obj_type": "Part::Common","obj_name": "Shared", "obj_properties": {"Base": "A",    "Tool": "B"}}
```

Useful types: `Part::Box`, `Part::Cylinder`, `Part::Sphere`, `Part::Cone`,
`Part::Torus`, `Part::Prism`, `Part::Wedge`, `Part::Mirroring`,
`Draft::Circle`, `PartDesign::Body`.

## execute_code

Runs on FreeCAD's GUI thread with a **90 second** budget, so document edits,
`recompute()`, and saves are all safe. `print()` output comes back in the
response — use it to return measurements instead of guessing.

```python
import FreeCAD
doc = FreeCAD.getDocument("Bracket")
box = doc.getObject("Base")
print(box.Shape.Volume, box.Shape.BoundBox)
doc.recompute()
doc.save()
```

Rules that matter:

- Always `doc.recompute()` after structural changes, and re-fetch objects by
  name rather than holding references across calls — each call is a fresh
  `exec`, so nothing but module globals survives.
- `doc.save()` only when the user asked for it or the file already has a path;
  a brand-new document needs `doc.saveAs("/abs/path.FCStd")`.
- Wrap risky sections in `try/except` and `print` the error — a raised
  exception returns the traceback but no screenshot.

### Sketch-based features

For anything with a profile, use PartDesign inside a `Body`:

```python
import FreeCAD, Part, Sketcher
doc = FreeCAD.getDocument("Bracket")
body = doc.addObject("PartDesign::Body", "Body")
sk = doc.addObject("Sketcher::SketchObject", "Profile")
body.addObject(sk)
sk.AttachmentSupport = [(doc.XY_Plane, "")]
sk.MapMode = "FlatFace"
sk.addGeometry(Part.LineSegment(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(50, 0, 0)), False)
# ... remaining segments, then close the wire
doc.recompute()
pad = doc.addObject("PartDesign::Pad", "Pad")
body.addObject(pad)
pad.Profile = sk
pad.Length = 12
doc.recompute()
print(pad.Shape.isValid())
```

Fillets and chamfers need edge names, which you can find by filtering the
shape's edges rather than hard-coding indices:

```python
edges = [f"Edge{i+1}" for i, e in enumerate(obj.Shape.Edges)
         if abs(e.BoundBox.ZLength) < 1e-6]
```

### execute_code_async

Returns immediately and runs off the GUI thread. Only for pure computation on
shapes you already hold — it must **not** touch `FreeCADGui`, the active view,
the selection, or the document tree. Stash results in a module-level global,
then apply them from a later `execute_code`. Background code cannot `print` to
the response; use `FreeCAD.Console.PrintMessage`.

## Exporting

```python
import Import, Mesh, FreeCAD
doc = FreeCAD.getDocument("Bracket")
Import.export([doc.getObject("Pocket")], "/abs/path/bracket.step")   # STEP
Mesh.export([doc.getObject("Pocket")], "/abs/path/bracket.stl")      # STL
```

Always use absolute paths — FreeCAD's working directory is not the project.

## FEM analysis

`run_fem_analysis(doc_name, analysis_name)` runs CalculiX and returns max von
Mises stress, max/min displacement, and node count. The document needs, in
order: a solid, a `Fem::AnalysisPython` container, a `Fem::MaterialCommon`, a
`Fem::FemMeshGmsh` (pass the solid as `Shape` plus
`CharacteristicLengthMax/Min`), and at least one `Fem::ConstraintFixed` and one
`Fem::ConstraintForce`. Every FEM object except the container takes
`analysis_name`. Constraints bind to faces via
`"References": [{"object_name": "Base", "face": "Face1"}]`.

The solve blocks every other RPC call for its duration — never fan out parallel
tool calls around it. See `examples/cantilever_fem.py` for a full run.

## Reporting back

Give the user the model's real numbers — overall dimensions, wall thickness,
hole positions — and name the objects you created so they can find them in the
tree. If a constraint from the request could not be met (a wall too thin to
print, a fillet larger than the edge), say which and what you did instead.
