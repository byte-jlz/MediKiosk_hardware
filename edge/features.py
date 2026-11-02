import numpy as np

def add_squares(X):
    X = np.asarray(X, dtype=float)
    return np.hstack([X, X ** 2])
