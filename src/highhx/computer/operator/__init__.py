"""Operators: how a vision model works a computer through HighhX.

    parse.py      a model's answer → one GuiAction (UI-TARS or JSON), validated, never executed
    computer.py   ComputerOperator: screenshots, GUI actions, state, focus, verification — all
                  through the action executor (approval, audit, capture grounding)
    vision.py     VisionAgent: goal → screenshot → model → action → new screenshot → … → verified
    models.py     which vision model (a local one keeps screenshots on this computer)
"""
