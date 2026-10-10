"""cascagent — multi-agent recursive task decomposition (protocol v2).

Stage 1 (Foundation): models (Task/TaskStatus), plugins (base classes +
OutputSplitterPlugin for think/response splitting + TaskParserPlugin —
decomposition parsing and duplicate detection, including the absorbed
DuplicateDetector rules), history. The former standalone parser.py module
is deleted.
"""

__version__ = "0.2.0"
