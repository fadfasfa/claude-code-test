import traceback
from backend_refresh import refresh_backend_data

try:
    result = refresh_backend_data(force=True)
    print(f'RESULT={result}')
except Exception:
    traceback.print_exc()
    raise
