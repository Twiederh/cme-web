/*
 * TLS-Platzhalter: libre ohne OpenSSL liefert auf Apple einige TLS-Funktionen nicht, baresip ruft sie
 * aber fest auf. CME Phone nutzt (noch) kein TLS – die Funktionen melden "nicht unterstützt".
 */
#include <re.h>

int tls_add_certf(struct tls *tls, const char *certf, const char *host)
{
	(void)tls; (void)certf; (void)host;
	return ENOSYS;
}

int tls_set_verify_client_handler(struct tls_conn *tc, int depth,
				  int (*verifyh) (int ok, void *arg), void *arg)
{
	(void)tc; (void)depth; (void)verifyh; (void)arg;
	return ENOSYS;
}

int tls_verify_client_post_handshake(struct tls_conn *tc)
{
	(void)tc;
	return ENOSYS;
}
