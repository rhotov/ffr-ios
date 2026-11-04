use std::{fs::File, io::Read, path::Path};

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];
struct Hash {
    h: [u32; 8],
    block: [u8; 64],
    used: usize,
    bytes: u64,
}
impl Hash {
    fn new() -> Self {
        Self {
            h: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
                0x5be0cd19,
            ],
            block: [0; 64],
            used: 0,
            bytes: 0,
        }
    }
    fn compress(&mut self, block: &[u8]) {
        let mut w = [0u32; 64];
        for (i, c) in block.as_chunks::<4>().0.iter().enumerate() {
            w[i] = u32::from_be_bytes(*c);
        }
        for i in 16..64 {
            let x = w[i - 15];
            let y = w[i - 2];
            let s0 = x.rotate_right(7) ^ x.rotate_right(18) ^ (x >> 3);
            let s1 = y.rotate_right(17) ^ y.rotate_right(19) ^ (y >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = self.h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let t1 = h
                .wrapping_add(s1)
                .wrapping_add((e & f) ^ (!e & g))
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let t2 = s0.wrapping_add((a & b) ^ (a & c) ^ (b & c));
            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        for (x, y) in self.h.iter_mut().zip([a, b, c, d, e, f, g, h]) {
            *x = x.wrapping_add(y);
        }
    }
    fn update(&mut self, mut b: &[u8]) {
        self.bytes += b.len() as u64;
        if self.used > 0 {
            let n = b.len().min(64 - self.used);
            self.block[self.used..self.used + n].copy_from_slice(&b[..n]);
            self.used += n;
            b = &b[n..];
            if self.used < 64 {
                return;
            }
            let block = self.block;
            self.compress(&block);
            self.used = 0;
        }
        while b.len() >= 64 {
            self.compress(&b[..64]);
            b = &b[64..];
        }
        self.block[..b.len()].copy_from_slice(b);
        self.used = b.len();
    }
    fn finish(mut self) -> [u8; 32] {
        let bits = self.bytes.checked_mul(8).expect("SHA256 input length");
        let mut tail = [0; 128];
        tail[..self.used].copy_from_slice(&self.block[..self.used]);
        tail[self.used] = 0x80;
        let n = if self.used < 56 { 64 } else { 128 };
        tail[n - 8..n].copy_from_slice(&bits.to_be_bytes());
        for c in tail[..n].as_chunks::<64>().0 {
            self.compress(c);
        }
        let mut out = [0; 32];
        for (i, h) in self.h.iter().enumerate() {
            out[i * 4..i * 4 + 4].copy_from_slice(&h.to_be_bytes());
        }
        out
    }
}
pub fn hex(b: &[u8]) -> String {
    const DIGITS: &[u8] = b"0123456789abcdef";
    let mut s = String::with_capacity(b.len() * 2);
    for v in b {
        s.push(DIGITS[(v >> 4) as usize] as char);
        s.push(DIGITS[(v & 15) as usize] as char);
    }
    s
}
pub fn hash_file(p: &Path) -> Result<[u8; 32], String> {
    let mut f = File::open(p).map_err(|e| format!("{}: {e}", p.display()))?;
    let mut hash = Hash::new();
    let mut block = [0; 65536];
    loop {
        let n = f.read(&mut block).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hash.update(&block[..n]);
    }
    Ok(hash.finish())
}
#[cfg(test)]
mod tests {
    use super::*;
    fn digest(b: &[u8], chunk: usize) -> String {
        let mut h = Hash::new();
        for c in b.chunks(chunk) {
            h.update(c);
        }
        hex(&h.finish())
    }
    #[test]
    fn standard_vectors_and_stream_boundaries() {
        assert_eq!(
            digest(b"", 1),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            digest(b"abc", 1),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let b = b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
        for chunk in [1, 7, 55, 56, 63, 64, 65] {
            assert_eq!(
                digest(b, chunk),
                "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
            );
        }
        assert_eq!(
            digest(&vec![b'a'; 1_000_000], 73),
            "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"
        );
    }
}
