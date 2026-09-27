"""HighhX Pro: the AI developer agent.

Layers (each usable on its own):

    model/        provider abstraction + adapters (HighhX gateway, Anthropic, OpenAI, Gemini)
    tools/        HighhX capabilities exposed to the model, validated and risk-gated
    permissions   path confinement, policy and approvals for agent actions
    planner       plans and live step progress
    context       project facts gathered at session start
    memory        durable per-project facts
    history       saved sessions and transcripts (state database)
    session       the agent loop
    ui / repl     the terminal experience and slash commands
    sync          session metadata sync to the HighhX platform
"""
