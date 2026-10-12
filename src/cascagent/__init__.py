"""cascagent — multi-agent recursive task decomposition (protocol v2).

Stage 1 (Foundation): models (Task/TaskStatus), plugins (base classes +
TaskParserPlugin — decomposition parsing and duplicate detection,
including the absorbed DuplicateDetector rules), history. The former
standalone parser.py module is deleted. Think/response splitting lives
neither in plugins nor in TaskParserPlugin: it is a built-in agent
service (StreamParser), not a plugin; OutputSplitterPlugin was removed.
"""

__version__ = "0.2.0"
