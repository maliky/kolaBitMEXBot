;; config file for my editor IDE (emacs)
((python-mode
  (python-black-command . "/home/mlk/.pyenv/shims/black")
  (python-shell-interpreter . "/home/mlk/.pyenv/shims/python3.8")
  (flycheck-python-mypy-executable . "/home/mlk/.pyenv/shims/mypy")
  (flycheck-python-flake8-executable . "/home/mlk/.pyenv/shims/flake8")

  (python-black--base-args . "--quiet --config ./setup.cfg")
  
  (flycheck-python-mypy-config . "./setup.cfg")
  (flycheck-flake8rc . "./setup.cfg")

  (python-shell-completion-native-enable . nil)))
