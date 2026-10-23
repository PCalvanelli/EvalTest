import os

os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
os.environ.setdefault('MLFLOW_DISABLE_AGENT_HINT', '1')  # MLflow logs a coding-assistant hint on import otherwise
