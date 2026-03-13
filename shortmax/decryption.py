from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import logging

logger = logging.getLogger(__name__)

class ShortmaxDecryptor:
    AES_IV = b'shortmax00000000'
    
    @staticmethod
    def decrypt_segment(data: bytes) -> bytes:
        """
        Decrypt a Shortmax HLS segment (.ts) using AES-128-CBC.
        Logic based on Next.js proxy implementation provided by user.
        """
        if not data:
            return data
            
        # Already a clean TS segment
        if data[0:1] == b'\x47':
            return data

        # Check for "shortmax" header
        if len(data) < 1040:
            return data
            
        try:
            magic = data[0:8].decode('ascii', errors='ignore')
            if magic != 'shortmax':
                return data
        except:
            return data

        try:
            # 1. Parse header: extract key position (bytes 16-20 as string)
            key_pos_bytes = data[16:20]
            if not key_pos_bytes:
                return data[1040:]
                
            key_pos_str = key_pos_bytes.decode('ascii', errors='ignore').strip()
            if not key_pos_str:
                return data[1040:]
                
            key_pos = int(key_pos_str)
            key_offset = key_pos - 24 # Convert absolute offset to key_data-relative

            # 2. Extract the 16-byte AES key from the key data
            # Key data starts at offset 24
            key_start = 24 + key_offset
            if len(data) < key_start + 16:
                 return data[1040:]
                 
            aes_key = data[key_start : key_start + 16]

            # 3. Assemble ciphertext: tail16 + first 1024 bytes of payload
            # tail16 is bytes 1024 to 1040
            # payload starts at 1040
            tail16 = data[1024:1040]
            payload = data[1040:]
            
            if len(payload) < 1024:
                return payload
                
            ciphertext = tail16 + payload[:1024]

            # 4. AES-128-CBC decrypt
            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(ShortmaxDecryptor.AES_IV), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(ciphertext) + decryptor.finalize()

            # 5. Verify TS sync byte
            if not decrypted or decrypted[0:1] != b'\x47':
                logger.warning('[shortmax_decrypt] TS sync byte (0x47) missing after decryption')
                return payload

            # 6. Combine: decrypted first 1024 bytes + plaintext remainder
            return decrypted + payload[1024:]
            
        except Exception as e:
            logger.error(f'[shortmax_decrypt] Error during decryption: {e}')
            # Fallback: strip header and return payload anyway
            if len(data) >= 1040:
                return data[1040:]
            return data
