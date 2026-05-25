import os
import sys
_basedir = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, _basedir)

from app import app, init_db
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    from waitress import serve
    serve(app, host='0.0.0.0', port=port)
