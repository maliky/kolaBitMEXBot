;; config file for my editor IDE (emacs)
((python-mode
  (python-black-command . "~/kolaBitMEXBot/bin/black")
  (python-shell-interpreter . "~/kolaBitMEXBot/bin/python3.8")
  (flycheck-python-mypy-executable . "~/kolaBitMEXBot/bin/mypy")
  (flycheck-python-flake8-executable . "~/kolaBitMEXBot/bin/flake8")

  (python-black--base-args . "--quiet --config ./setup.cfg")
  (blacken-skip-string-normalization nil)
  (flycheck-python-mypy-config . "./setup.cfg")
  (flycheck-flake8rc . "./setup.cfg")

  (python-shell-completion-native-enable . nil)))
