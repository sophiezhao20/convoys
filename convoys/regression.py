import numpy # TODO: remove
from scipy.special import expit, gammainc  # TODO: remove
import scipy.stats
import tensorflow as tf
import sys

from convoys.model import Model


tf.logging.set_verbosity(2)

def _get_placeholders(n, k):
    return (
        tf.placeholder(tf.float32, [n, k]),
        tf.placeholder(tf.float32, [n]),
        tf.placeholder(tf.float32, [n])
    )


def _optimize(sess, target, feed_dict):
    learning_rate_input = tf.placeholder(tf.float32, [])
    optimizer = tf.train.AdamOptimizer(learning_rate_input).minimize(-target)

    # TODO(erikbern): this is going to add more and more variables every time we run this
    sess.run(tf.global_variables_initializer())

    best_cost, best_step, step = float('-inf'), 0, 0
    learning_rate = 0.1
    while True:
        feed_dict[learning_rate_input] = learning_rate
        sess.run(optimizer, feed_dict=feed_dict)
        cost = sess.run(target, feed_dict=feed_dict)
        if cost > best_cost:
            best_cost, best_step = cost, step
        if step - best_step > 40:
            learning_rate /= 10
            best_cost = float('-inf')
        if learning_rate < 1e-6:
            break
        step += 1
        sys.stdout.write('step %6d (lr %6.6f): %12.4f' % (step, learning_rate, cost))
        sys.stdout.write('\n' if step % 100 == 0 else '\r')
        sys.stdout.flush()


def _get_params(sess, params):
    return {key: sess.run(param) for key, param in params.items()}


def _get_hessian(sess, f, param, feed_dict):
    return sess.run(tf.hessians(-f, [param]), feed_dict=feed_dict)[0]


def _fix_t(t):
    # TODO: this is stupid, should at least have tests for it
    t = numpy.array(t)
    if len(t.shape) == 0:
        return t
    elif len(t.shape) == 1:
        return numpy.array([[z] for z in t])
    else:
        return t


def _sample_hessian(x, value, hessian, n, ci):
    if ci is None:
        return numpy.dot(x, value)
    else:
        x = numpy.array(x)
        inv_var = numpy.dot(numpy.dot(x.T, hessian), x)
        return numpy.dot(x, value) + scipy.stats.norm.rvs(scale=inv_var**-0.5, size=(1, n))


def _predict(func_values, ci):
    if ci is None:
        return func_values
    else:
        axis = len(func_values.shape)-1
        return numpy.mean(func_values, axis=axis), numpy.percentile(func_values, (1-ci)*50, axis=axis), numpy.percentile(func_values, (1+ci)*50, axis=axis)



class Regression(Model):
    def __init__(self, L2_reg=1.0):
        self._L2_reg = L2_reg

    def predict_time(self):
        pass  # TODO: implement


class ExponentialRegression(Regression):
    def fit(self, X, B, T):
        n, k = X.shape
        X_input, B_input, T_input = _get_placeholders(n, k)

        alpha = tf.Variable(tf.zeros([k]), 'alpha')
        beta = tf.Variable(tf.zeros([k]), 'beta')
        X_prod_alpha = tf.squeeze(tf.matmul(X_input, tf.expand_dims(alpha, -1)), 1)
        X_prod_beta = tf.squeeze(tf.matmul(X_input, tf.expand_dims(beta, -1)), 1)
        lambd = tf.exp(X_prod_alpha)
        c = tf.sigmoid(X_prod_beta)

        log_pdf = lambda T: tf.log(lambd) - T*lambd
        cdf = lambda T: 1 - tf.exp(-(T * lambd))

        LL_observed = tf.log(c) + log_pdf(T_input)
        LL_censored = tf.log((1-c) + c * (1 - cdf(T_input)))

        LL = tf.reduce_sum(B_input * LL_observed + (1 - B_input) * LL_censored, 0)
        LL_penalized = LL - self._L2_reg * tf.reduce_sum(beta * beta, 0)

        with tf.Session() as sess:
            feed_dict = {X_input: X, B_input: B, T_input: T}
            _optimize(sess, LL_penalized, feed_dict)
            self.params = _get_params(sess, {'beta': beta, 'alpha': alpha})
            self.params['alpha_hessian'] = _get_hessian(sess, LL_penalized, alpha, feed_dict)
            self.params['beta_hessian'] = _get_hessian(sess, LL_penalized, beta, feed_dict)

    def predict(self, x, t, ci=None, n=1000):
        t = _fix_t(t)
        kappa = _sample_hessian(x, self.params['beta'], self.params['beta_hessian'], n, ci)
        lambd = _sample_hessian(x, self.params['alpha'], self.params['alpha_hessian'], n, ci)
        return _predict(expit(kappa) * (1 - numpy.exp(-t * numpy.exp(lambd))), ci)

    def predict_final(self, x, ci=None, n=1000):
        kappa = _sample_hessian(x, self.params['beta'], self.params['beta_hessian'], n, ci)
        return _predict(expit(kappa), ci)


class WeibullRegression(Regression):
    def fit(self, X, B, T):
        n, k = X.shape
        X_input, B_input, T_input = _get_placeholders(n, k)

        alpha = tf.Variable(tf.zeros([k]), 'alpha')
        beta = tf.Variable(tf.zeros([k]), 'beta')
        log_k_var = tf.Variable(tf.zeros([]), 'log_k')
        X_prod_alpha = tf.squeeze(tf.matmul(X_input, tf.expand_dims(alpha, -1)), 1)
        X_prod_beta = tf.squeeze(tf.matmul(X_input, tf.expand_dims(beta, -1)), 1)
        k = tf.exp(log_k_var)
        lambd = tf.exp(X_prod_alpha)
        c = tf.sigmoid(X_prod_beta)

        # PDF of Weibull: k * lambda * (x * lambda)^(k-1) * exp(-(t * lambda)^k)
        log_pdf = lambda T: tf.log(k) + tf.log(lambd) + (k-1)*(tf.log(T) + tf.log(lambd)) - (T*lambd)**k
        # CDF of Weibull: 1 - exp(-(t * lambda)^k)
        cdf = lambda T: 1 - tf.exp(-(T * lambd)**k)

        LL_observed = tf.log(c) + log_pdf(T_input)
        LL_censored = tf.log((1-c) + c * (1 - cdf(T_input)))

        LL = tf.reduce_sum(B_input * LL_observed + (1 - B_input) * LL_censored, 0)
        LL_penalized = LL - self._L2_reg * tf.reduce_sum(beta * beta, 0)

        with tf.Session() as sess:
            feed_dict = {X_input: X, B_input: B, T_input: T}
            _optimize(sess, LL_penalized, feed_dict)
            self.params = _get_params(sess, {'beta': beta, 'alpha': alpha, 'k': k})
            self.params['alpha_hessian'] = _get_hessian(sess, LL_penalized, alpha, feed_dict)
            self.params['beta_hessian'] = _get_hessian(sess, LL_penalized, beta, feed_dict)

    def predict(self, x, t, ci=None, n=1000):
        t = _fix_t(t)
        kappa = _sample_hessian(x, self.params['beta'], self.params['beta_hessian'], n, ci)
        lambd = _sample_hessian(x, self.params['alpha'], self.params['alpha_hessian'], n, ci)
        return _predict(expit(kappa) * (1 - numpy.exp(-(t * numpy.exp(lambd))**self.params['k'])), ci)

    def predict_final(self, x, ci=None, n=1000):
        kappa = _sample_hessian(x, self.params['beta'], self.params['beta_hessian'], n, ci)
        return _predict(expit(kappa), ci)


class GammaRegression(Regression):
    def fit(self, X, B, T):
        n, k = X.shape
        X_input, B_input, T_input = _get_placeholders(n, k)

        alpha = tf.Variable(tf.zeros([k]), 'alpha')
        beta = tf.Variable(tf.zeros([k]), 'beta')
        log_k_var = tf.Variable(tf.zeros([]), 'log_k')
        X_prod_alpha = tf.squeeze(tf.matmul(X_input, tf.expand_dims(alpha, -1)), 1)
        X_prod_beta = tf.squeeze(tf.matmul(X_input, tf.expand_dims(beta, -1)), 1)
        k = tf.exp(log_k_var)
        lambd = tf.exp(X_prod_alpha)
        c = tf.sigmoid(X_prod_beta)

        # PDF of gamma: 1.0 / gamma(k) * lambda ^ k * t^(k-1) * exp(-t * lambda)
        log_pdf = lambda T: -tf.lgamma(k) + k*tf.log(lambd) + (k-1)*tf.log(T) - lambd*T
        # CDF of gamma: gammainc(k, lambda * t)
        cdf = lambda T: tf.igamma(k, lambd * T)

        LL_observed = tf.log(c) + log_pdf(T_input)
        LL_censored = tf.log((1-c) + c * (1 - cdf(T_input)))

        LL = tf.reduce_sum(B_input * LL_observed + (1 - B_input) * LL_censored, 0)
        LL_penalized = LL - self._L2_reg * tf.reduce_sum(beta * beta, 0)

        with tf.Session() as sess:
            feed_dict = {X_input: X, B_input: B, T_input: T}
            _optimize(sess, LL_penalized, feed_dict)
            self.params = _get_params(sess, {'beta': beta, 'alpha': alpha, 'k': k})
            self.params['alpha_hessian'] = _get_hessian(sess, LL_penalized, alpha, feed_dict)
            self.params['beta_hessian'] = _get_hessian(sess, LL_penalized, beta, feed_dict)

    def predict(self, x, t, ci=None, n=1000):
        t = _fix_t(t)
        kappa = _sample_hessian(x, self.params['beta'], self.params['beta_hessian'], n, ci)
        lambd = _sample_hessian(x, self.params['alpha'], self.params['alpha_hessian'], n, ci)
        return _predict(expit(kappa) * (1 - gammainc(self.params['k'], t * numpy.exp(lambd))), ci)

    def predict_final(self, x, ci=None, n=1000):
        kappa = _sample_hessian(x, self.params['beta'], self.params['beta_hessian'], n, ci)
        return _predict(expit(kappa), ci)
