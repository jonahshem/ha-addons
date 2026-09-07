"""The add-on: one AES67 stream, and an API that speaks into it."""
import threading

import api
import sender

pipe = sender.build()
threading.Thread(target=api.serve, daemon=True).start()
sender.run_forever(pipe)
