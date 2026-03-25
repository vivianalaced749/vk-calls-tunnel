package dtls

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"

	"github.com/pion/dtls/v3"
)

// Config holds DTLS configuration options.
type Config struct {
	PSK         []byte
	PSKIdentity string
}

// DefaultConfig returns sensible DTLS defaults.
func DefaultConfig() *Config {
	return &Config{}
}

func generateSelfSigned() (tls.Certificate, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, err
	}

	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
	}

	certDER, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, err
	}

	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return tls.Certificate{}, err
	}

	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})

	return tls.X509KeyPair(certPEM, keyPEM)
}

func buildConfig(cfg *Config, isServer bool) (*dtls.Config, error) {
	dtlsCfg := &dtls.Config{
		InsecureSkipVerify: true,
		MTU:                1200,
		CipherSuites:       []dtls.CipherSuiteID{dtls.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256},
	}

	// Connection ID: critical for multiplexing through TURN
	// Server uses random 8-byte CIDs, client sends-only
	if isServer {
		dtlsCfg.ConnectionIDGenerator = dtls.RandomCIDGenerator(8)
	} else {
		dtlsCfg.ConnectionIDGenerator = dtls.OnlySendCIDGenerator()
	}

	if len(cfg.PSK) > 0 {
		psk := cfg.PSK
		dtlsCfg.PSK = func(hint []byte) ([]byte, error) {
			return psk, nil
		}
		dtlsCfg.PSKIdentityHint = []byte(cfg.PSKIdentity)
		dtlsCfg.CipherSuites = []dtls.CipherSuiteID{dtls.TLS_PSK_WITH_AES_128_GCM_SHA256}
	} else {
		cert, err := generateSelfSigned()
		if err != nil {
			return nil, fmt.Errorf("generate cert: %w", err)
		}
		dtlsCfg.Certificates = []tls.Certificate{cert}
	}

	return dtlsCfg, nil
}

// Client wraps a PacketConn with client-side DTLS.
func Client(conn net.PacketConn, rAddr net.Addr, cfg *Config) (*dtls.Conn, error) {
	if cfg == nil {
		cfg = DefaultConfig()
	}
	dtlsCfg, err := buildConfig(cfg, false)
	if err != nil {
		return nil, err
	}
	return dtls.Client(conn, rAddr, dtlsCfg)
}

// Listen creates a DTLS server listener on UDP.
func Listen(addr string, cfg *Config) (net.Listener, error) {
	if cfg == nil {
		cfg = DefaultConfig()
	}
	dtlsCfg, err := buildConfig(cfg, true)
	if err != nil {
		return nil, err
	}
	udpAddr, err := net.ResolveUDPAddr("udp", addr)
	if err != nil {
		return nil, err
	}
	return dtls.Listen("udp", udpAddr, dtlsCfg)
}
